import datetime
import json
from calendar import Calendar, SUNDAY
from collections import defaultdict, namedtuple
from datetime import date, time, timedelta
from functools import partial
from itertools import chain
from operator import attrgetter, itemgetter

from django import forms
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import BooleanField, Case, Count, F, FloatField, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.expressions import CombinedExpression, Exists, OuterRef
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import date as date_filter
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.timezone import make_aware
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import ListView, TemplateView, View
from django.views.generic.detail import DetailView, SingleObjectMixin
from django.views.generic.list import BaseListView
from icalendar import Calendar as ICalendar, Event
from reversion import revisions

from judge import event_poster as event
from judge.comments import CommentedDetailView
from judge.forms import ContestCloneForm
from judge.models import Contest, ContestMoss, ContestParticipation, ContestProblem, ContestTag, \
    Problem, Profile, Submission
from judge.tasks import run_moss
from judge.utils.celery import redirect_to_task_status
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import _get_result_data
from judge.utils.ranker import ranker
from judge.utils.stats import get_bar_chart, get_pie_chart
from judge.utils.views import DiggPaginatorMixin, QueryStringSortMixin, SingleObjectFormView, TitleMixin, \
    generic_message

__all__ = ['ContestList', 'ContestDetail', 'ContestRanking', 'ContestJoin', 'ContestLeave', 'ContestCalendar',
           'ContestClone', 'ContestStats', 'ContestMossView', 'ContestMossDelete', 'contest_ranking_ajax',
           'ContestParticipationList', 'ContestParticipationDisqualify', 'get_contest_ranking_list',
           'base_contest_ranking_list', 'ContestCreateView']


def _find_contest(request, key, private_check=True):
    try:
        contest = Contest.objects.get(key=key)
        if private_check and not contest.is_accessible_by(request.user):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return generic_message(request, _('No such contest'),
                               _('Could not find a contest with the key "%s".') % key, status=404), False
    return contest, True


class ContestListMixin(object):
    def get_queryset(self):
        return Contest.get_visible_contests(self.request.user)


class ContestList(QueryStringSortMixin, DiggPaginatorMixin, TitleMixin, ContestListMixin, ListView):
    model = Contest
    paginate_by = 20
    template_name = 'contest/list.html'
    title = gettext_lazy('Contests')
    context_object_name = 'past_contests'
    all_sorts = frozenset(('name', 'user_count', 'start_time'))
    default_desc = frozenset(('name', 'user_count'))
    default_sort = '-start_time'

    @cached_property
    def _now(self):
        return timezone.now()

    def _get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'tags',
            'organizations',
            'authors',
            'curators',
            'testers',
            'spectators',
            'classes',
        )

        profile = self.request.profile
        if not profile:
            return queryset

        return queryset.annotate(
            editor_or_tester=Exists(Contest.authors.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.curators.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.testers.through.objects.filter(contest=OuterRef('pk'), profile=profile)),
            completed_contest=Exists(ContestParticipation.objects.filter(contest=OuterRef('pk'), user=profile,
                                                                         virtual=ContestParticipation.LIVE)),
        )

    def get_queryset(self):
        self.search_query = None
        queryset = self._get_queryset().order_by(self.order, 'key').filter(end_time__lt=self._now)
        if 'search' in self.request.GET:
            self.search_query = search_query = ' '.join(self.request.GET.getlist('search')).strip()
            if search_query:
                queryset = queryset.filter(Q(key__icontains=search_query) | Q(name__icontains=search_query))
        return queryset

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        return super().get_paginator(queryset, per_page, orphans, allow_empty_first_page,
                                     count=self.get_queryset().values('id').count(), **kwargs)

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)
        present, active, future = [], [], []
        finished = set()
        for contest in self._get_queryset().exclude(end_time__lt=self._now):
            if contest.start_time > self._now:
                future.append(contest)
            else:
                present.append(contest)

        if self.request.user.is_authenticated:
            for participation in (
                ContestParticipation.objects.filter(virtual=0, user=self.request.profile, contest_id__in=present)
                .select_related('contest')
                .prefetch_related('contest__authors', 'contest__curators', 'contest__testers', 'contest__spectators')
                .annotate(key=F('contest__key'))
            ):
                if participation.ended:
                    finished.add(participation.contest.key)
                else:
                    active.append(participation)
                    present.remove(participation.contest)

        active.sort(key=attrgetter('end_time', 'key'))
        present.sort(key=attrgetter('end_time', 'key'))
        future.sort(key=attrgetter('start_time'))
        context['active_participations'] = active
        context['current_contests'] = present
        context['future_contests'] = future
        context['finished_contests'] = finished
        context['now'] = self._now
        context['first_page_href'] = '.'
        context['page_suffix'] = '#past-contests'
        context['search_query'] = self.search_query
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


class PrivateContestError(Exception):
    def __init__(self, name, is_private, is_organization_private, orgs, classes):
        self.name = name
        self.is_private = is_private
        self.is_organization_private = is_organization_private
        self.orgs = orgs
        self.classes = classes


class ContestMixin(object):
    context_object_name = 'contest'
    model = Contest
    slug_field = 'key'
    slug_url_kwarg = 'contest'

    @cached_property
    def is_editor(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.editor_ids

    @cached_property
    def is_tester(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.tester_ids

    @cached_property
    def is_spectator(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.spectator_ids

    @cached_property
    def can_edit(self):
        return self.object.is_editable_by(self.request.user)

    def get_context_data(self, **kwargs):
        context = super(ContestMixin, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            try:
                context['live_participation'] = (
                    self.request.profile.contest_history.get(
                        contest=self.object,
                        virtual=ContestParticipation.LIVE,
                    )
                )
            except ContestParticipation.DoesNotExist:
                context['live_participation'] = None
                context['has_joined'] = False
            else:
                context['has_joined'] = True
        else:
            context['live_participation'] = None
            context['has_joined'] = False

        context['now'] = timezone.now()
        context['is_editor'] = self.is_editor
        context['is_tester'] = self.is_tester
        context['is_spectator'] = self.is_spectator
        context['can_edit'] = self.can_edit

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph('generated-meta-contest:%d' % self.object.id,
                                          self.object.description, 'contest')
        context['meta_description'] = self.object.summary or metadata[0]
        context['og_image'] = self.object.og_image or metadata[1]
        context['has_moss_api_key'] = settings.MOSS_API_KEY is not None
        context['logo_override_image'] = self.object.logo_override_image
        if not context['logo_override_image'] and self.object.organizations.count() == 1:
            context['logo_override_image'] = self.object.organizations.first().logo_override_image

        return context

    def get_object(self, queryset=None):
        contest = super(ContestMixin, self).get_object(queryset)

        profile = self.request.profile
        if (profile is not None and
                ContestParticipation.objects.filter(id=profile.current_contest_id, contest_id=contest.id).exists()):
            return contest

        try:
            contest.access_check(self.request.user)
        except Contest.PrivateContest:
            raise PrivateContestError(contest.name, contest.is_private, contest.is_organization_private,
                                      contest.organizations.all(), contest.classes.all())
        except Contest.Inaccessible:
            raise Http404()
        else:
            return contest

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(ContestMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, _('No such contest'),
                                       _('Could not find a contest with the key "%s".') % key)
            else:
                return generic_message(request, _('No such contest'),
                                       _('Could not find such contest.'))
        except PrivateContestError as e:
            return render(request, 'contest/private.html', {
                'error': e, 'title': _('Access to contest "%s" denied') % e.name,
            }, status=403)


class ContestDetail(ContestMixin, TitleMixin, CommentedDetailView):
    template_name = 'contest/contest.html'

    def get_comment_page(self):
        return 'c:%s' % self.object.key

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        context['contest_problems'] = Problem.objects.filter(contests__contest=self.object) \
            .order_by('contests__order').defer('description') \
            .annotate(has_public_editorial=Case(
                When(solution__is_public=True, solution__publish_on__lte=timezone.now(), then=True),
                default=False,
                output_field=BooleanField(),
            )) \
            .add_i18n_name(self.request.LANGUAGE_CODE)
        context['metadata'] = {
            'has_public_editorials': any(
                problem.is_public and problem.has_public_editorial for problem in context['contest_problems']
            ),
        }
        context['metadata'].update(
            **self.object.contest_problems
            .annotate(
                partials_enabled=Case(
                    When(partial=True, problem__partial=True, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
                pretests_enabled=Case(
                    When(is_pretested=True, contest__run_pretests_only=True, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
            )
            .aggregate(
                has_partials=Sum('partials_enabled'),
                has_pretests=Sum('pretests_enabled'),
                has_submission_cap=Sum('max_submissions'),
                problem_count=Count('id'),
            ),
        )
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS
        context['enable_social'] = settings.DMOJ_ENABLE_SOCIAL
        return context


class ContestClone(ContestMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView):
    title = gettext_lazy('Clone Contest')
    template_name = 'contest/clone.html'
    form_class = ContestCloneForm
    permission_required = 'judge.clone_contest'

    def form_valid(self, form):
        contest = self.object

        tags = contest.tags.all()
        organizations = contest.organizations.all()
        private_contestants = contest.private_contestants.all()
        view_contest_scoreboard = contest.view_contest_scoreboard.all()
        contest_problems = contest.contest_problems.all()
        old_key = contest.key

        contest.pk = None
        contest.is_visible = False
        contest.user_count = 0
        contest.locked_after = None
        contest.key = form.cleaned_data['key']
        with revisions.create_revision(atomic=True):
            contest.save()
            contest.tags.set(tags)
            contest.organizations.set(organizations)
            contest.private_contestants.set(private_contestants)
            contest.view_contest_scoreboard.set(view_contest_scoreboard)
            contest.authors.add(self.request.profile)

            for problem in contest_problems:
                problem.contest = contest
                problem.pk = None
            ContestProblem.objects.bulk_create(contest_problems)

            revisions.set_user(self.request.user)
            revisions.set_comment(_('Cloned contest from %s') % old_key)

        return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(contest.id,)))


class ContestAccessDenied(Exception):
    pass


class ContestAccessCodeForm(forms.Form):
    access_code = forms.CharField(max_length=255)

    def __init__(self, *args, **kwargs):
        super(ContestAccessCodeForm, self).__init__(*args, **kwargs)
        self.fields['access_code'].widget.attrs.update({'autocomplete': 'off'})


class ContestJoin(LoginRequiredMixin, ContestMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.ask_for_access_code()

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            return self.join_contest(request)
        except ContestAccessDenied:
            if request.POST.get('access_code'):
                return self.ask_for_access_code(ContestAccessCodeForm(request.POST))
            else:
                return HttpResponseRedirect(request.path)

    def join_contest(self, request, access_code=None):
        contest = self.object

        if not contest.started and not (self.is_editor or self.is_tester):
            return generic_message(request, _('Contest not ongoing'),
                                   _('"%s" is not currently ongoing.') % contest.name)

        profile = request.profile

        if not request.user.is_superuser and contest.banned_users.filter(id=profile.id).exists():
            return generic_message(request, _('Banned from joining'),
                                   _('You have been declared persona non grata for this contest. '
                                     'You are permanently barred from joining this contest.'))

        requires_access_code = (not self.can_edit and contest.access_code and access_code != contest.access_code)
        if contest.ended:
            if requires_access_code:
                raise ContestAccessDenied()

            while True:
                virtual_id = max((ContestParticipation.objects.filter(contest=contest, user=profile)
                                  .aggregate(virtual_id=Max('virtual'))['virtual_id'] or 0) + 1, 1)
                try:
                    participation = ContestParticipation.objects.create(
                        contest=contest, user=profile, virtual=virtual_id,
                        real_start=timezone.now(),
                    )
                # There is obviously a race condition here, so we keep trying until we win the race.
                except IntegrityError:
                    pass
                else:
                    break
        else:
            SPECTATE = ContestParticipation.SPECTATE
            LIVE = ContestParticipation.LIVE

            if contest.is_live_joinable_by(request.user):
                participation_type = LIVE
            elif contest.is_spectatable_by(request.user):
                participation_type = SPECTATE
            else:
                return generic_message(request, _('Cannot enter'),
                                       _('You are not able to join this contest.'))
            try:
                participation = ContestParticipation.objects.get(
                    contest=contest, user=profile, virtual=participation_type,
                )
            except ContestParticipation.DoesNotExist:
                if requires_access_code:
                    raise ContestAccessDenied()

                participation = ContestParticipation.objects.create(
                    contest=contest, user=profile, virtual=participation_type,
                    real_start=timezone.now(),
                )
            else:
                if participation.ended:
                    participation = ContestParticipation.objects.get_or_create(
                        contest=contest, user=profile, virtual=SPECTATE,
                        defaults={'real_start': timezone.now()},
                    )[0]

        profile.current_contest = participation
        profile.save()
        contest._updating_stats_only = True
        contest.update_user_count()
        return HttpResponseRedirect(reverse('problem_list'))

    def ask_for_access_code(self, form=None):
        contest = self.object
        wrong_code = False
        if form:
            if form.is_valid():
                if form.cleaned_data['access_code'] == contest.access_code:
                    return self.join_contest(self.request, form.cleaned_data['access_code'])
                wrong_code = True
        else:
            form = ContestAccessCodeForm()
        return render(self.request, 'contest/access_code.html', {
            'form': form, 'wrong_code': wrong_code,
            'title': _('Enter access code for "%s"') % contest.name,
        })


class ContestLeave(LoginRequiredMixin, ContestMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        contest = self.get_object()

        profile = request.profile
        if profile.current_contest is None or profile.current_contest.contest_id != contest.id:
            return generic_message(request, _('No such contest'),
                                   _('You are not in contest "%s".') % contest.key, 404)

        profile.remove_contest()
        return HttpResponseRedirect(reverse('contest_view', args=(contest.key,)))


ContestDay = namedtuple('ContestDay', 'date is_pad is_today starts ends oneday')


class ContestCalendar(TitleMixin, ContestListMixin, TemplateView):
    firstweekday = SUNDAY
    template_name = 'contest/calendar.html'

    def get(self, request, *args, **kwargs):
        try:
            self.year = int(kwargs['year'])
            self.month = int(kwargs['month'])
        except (KeyError, ValueError):
            raise ImproperlyConfigured('ContestCalendar requires integer year and month')
        self.today = timezone.now().date()
        return self.render()

    def render(self):
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_contest_data(self, start, end):
        end += timedelta(days=1)
        contests = self.get_queryset().filter(Q(start_time__gte=start, start_time__lt=end) |
                                              Q(end_time__gte=start, end_time__lt=end))
        starts, ends, oneday = (defaultdict(list) for i in range(3))
        for contest in contests:
            start_date = timezone.localtime(contest.start_time).date()
            end_date = timezone.localtime(contest.end_time - timedelta(seconds=1)).date()
            if start_date == end_date:
                oneday[start_date].append(contest)
            else:
                starts[start_date].append(contest)
                ends[end_date].append(contest)
        return starts, ends, oneday

    def get_table(self):
        calendar = Calendar(self.firstweekday).monthdatescalendar(self.year, self.month)
        starts, ends, oneday = self.get_contest_data(make_aware(datetime.datetime.combine(calendar[0][0], time.min)),
                                                     make_aware(datetime.datetime.combine(calendar[-1][-1], time.min)))
        return [[ContestDay(
            date=date, is_pad=date.month != self.month,
            is_today=date == self.today, starts=starts[date], ends=ends[date], oneday=oneday[date],
        ) for date in week] for week in calendar]

    def get_context_data(self, **kwargs):
        context = super(ContestCalendar, self).get_context_data(**kwargs)

        try:
            month = date(self.year, self.month, 1)
        except ValueError:
            raise Http404()
        else:
            context['title'] = _('Contests in %(month)s') % {'month': date_filter(month, _('F Y'))}

        dates = Contest.objects.aggregate(min=Min('start_time'), max=Max('end_time'))
        min_month = (self.today.year, self.today.month)
        if dates['min'] is not None:
            min_month = dates['min'].year, dates['min'].month
        max_month = (self.today.year, self.today.month)
        if dates['max'] is not None:
            max_month = max((dates['max'].year, dates['max'].month), (self.today.year, self.today.month))

        month = (self.year, self.month)
        if month < min_month or month > max_month:
            # 404 is valid because it merely declares the lack of existence, without any reason
            raise Http404()

        context['now'] = timezone.now()
        context['calendar'] = self.get_table()
        context['curr_month'] = date(self.year, self.month, 1)

        if month > min_month:
            context['prev_month'] = date(self.year - (self.month == 1), 12 if self.month == 1 else self.month - 1, 1)
        else:
            context['prev_month'] = None

        if month < max_month:
            context['next_month'] = date(self.year + (self.month == 12), 1 if self.month == 12 else self.month + 1, 1)
        else:
            context['next_month'] = None
        return context


class ContestICal(TitleMixin, ContestListMixin, BaseListView):
    def generate_ical(self):
        cal = ICalendar()
        cal.add('prodid', '-//DMOJ//NONSGML Contests Calendar//')
        cal.add('version', '2.0')

        now = timezone.now().astimezone(datetime.timezone.utc)
        domain = self.request.get_host()
        for contest in self.get_queryset():
            event = Event()
            event.add('uid', f'contest-{contest.key}@{domain}')
            event.add('summary', contest.name)
            event.add('location', self.request.build_absolute_uri(contest.get_absolute_url()))
            event.add('dtstart', contest.start_time.astimezone(datetime.timezone.utc))
            event.add('dtend', contest.end_time.astimezone(datetime.timezone.utc))
            event.add('dtstamp', now)
            cal.add_component(event)
        return cal.to_ical()

    def render_to_response(self, context, **kwargs):
        return HttpResponse(self.generate_ical(), content_type='text/calendar')


class ContestStats(TitleMixin, ContestMixin, DetailView):
    template_name = 'contest/stats.html'

    def get_title(self):
        return _('%s Statistics') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not (self.object.ended or self.can_edit):
            raise Http404()

        queryset = Submission.objects.filter(contest_object=self.object)

        ac_count = Count(Case(When(result='AC', then=Value(1)), output_field=IntegerField()))
        ac_rate = CombinedExpression(ac_count / Count('problem'), '*', Value(100.0), output_field=FloatField())

        status_count_queryset = list(
            queryset.values('problem__code', 'result').annotate(count=Count('result'))
                    .values_list('problem__code', 'result', 'count'),
        )
        labels, codes = [], []
        contest_problems = self.object.contest_problems.order_by('order').values_list('problem__name', 'problem__code')
        if contest_problems:
            labels, codes = zip(*contest_problems)
        num_problems = len(labels)
        status_counts = [[] for i in range(num_problems)]
        for problem_code, result, count in status_count_queryset:
            if problem_code in codes:
                status_counts[codes.index(problem_code)].append((result, count))

        result_data = defaultdict(partial(list, [0] * num_problems))
        for i in range(num_problems):
            for category in _get_result_data(defaultdict(int, status_counts[i]))['categories']:
                result_data[category['code']][i] = category['count']

        stats = {
            'problem_status_count': {
                'labels': labels,
                'datasets': [
                    {
                        'label': name,
                        'backgroundColor': settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS[name],
                        'data': data,
                    }
                    for name, data in result_data.items()
                ],
            },
            'problem_ac_rate': get_bar_chart(
                queryset.values('contest__problem__order', 'problem__name').annotate(ac_rate=ac_rate)
                        .order_by('contest__problem__order').values_list('problem__name', 'ac_rate'),
            ),
            'language_count': get_pie_chart(
                queryset.values('language__name').annotate(count=Count('language__name'))
                        .filter(count__gt=0).order_by('-count').values_list('language__name', 'count'),
            ),
            'language_ac_rate': get_bar_chart(
                queryset.values('language__name').annotate(ac_rate=ac_rate)
                        .filter(ac_rate__gt=0).values_list('language__name', 'ac_rate'),
            ),
        }

        context['stats'] = mark_safe(json.dumps(stats))

        return context


ContestRankingProfile = namedtuple(
    'ContestRankingProfile',
    'id user css_class username points cumtime tiebreaker organization participation '
    'participation_rating problem_cells result_cell display_name',
)

BestSolutionData = namedtuple('BestSolutionData', 'code points time state is_pretested')


def make_contest_ranking_profile(contest, participation, contest_problems):
    def display_user_problem(contest_problem):
        # When the contest format is changed, `format_data` might be invalid.
        # This will cause `display_user_problem` to error, so we display '???' instead.
        try:
            return contest.format.display_user_problem(participation, contest_problem)
        except (KeyError, TypeError, ValueError):
            return mark_safe('<td>???</td>')

    user = participation.user
    return ContestRankingProfile(
        id=user.id,
        user=user.user,
        css_class=user.css_class,
        username=user.username,
        points=participation.score,
        cumtime=participation.cumtime,
        tiebreaker=participation.tiebreaker,
        organization=user.organization,
        participation_rating=participation.rating.rating if hasattr(participation, 'rating') else None,
        problem_cells=[display_user_problem(contest_problem) for contest_problem in contest_problems],
        result_cell=contest.format.display_participation_result(participation),
        participation=participation,
        display_name=user.display_name,
    )


def base_contest_ranking_list(contest, problems, queryset):
    return [make_contest_ranking_profile(contest, participation, problems) for participation in
            queryset.select_related('user__user', 'rating').defer('user__about', 'user__organizations__about')]


def contest_ranking_list(contest, problems):
    return base_contest_ranking_list(contest, problems, contest.users.filter(virtual=0)
                                     .prefetch_related('user__organizations')
                                     .annotate(submission_cnt=Count('submission'))
                                     .order_by('is_disqualified', '-score', 'cumtime', 'tiebreaker', '-submission_cnt'))


def get_contest_ranking_list(request, contest, participation=None, ranking_list=contest_ranking_list,
                             show_current_virtual=True, ranker=ranker):
    problems = list(contest.contest_problems.select_related('problem').defer('problem__description').order_by('order'))

    users = ranker(ranking_list(contest, problems), key=attrgetter('points', 'cumtime', 'tiebreaker'))

    if show_current_virtual:
        if participation is None and request.user.is_authenticated:
            participation = request.profile.current_contest
            if participation is None or participation.contest_id != contest.id:
                participation = None
        if participation is not None and participation.virtual:
            users = chain([('-', make_contest_ranking_profile(contest, participation, problems))], users)
    return users, problems


def contest_ranking_ajax(request, contest, participation=None):
    contest, exists = _find_contest(request, contest)
    if not exists:
        return HttpResponseBadRequest('Invalid contest', content_type='text/plain')

    if not contest.can_see_full_scoreboard(request.user):
        raise Http404()

    users, problems = get_contest_ranking_list(request, contest, participation)
    return render(request, 'contest/ranking-table.html', {
        'users': users,
        'problems': problems,
        'contest': contest,
        'has_rating': contest.ratings.exists(),
    })


class ContestRankingBase(ContestMixin, TitleMixin, DetailView):
    template_name = 'contest/ranking.html'
    tab = None

    def get_title(self):
        raise NotImplementedError()

    def get_content_title(self):
        return self.object.name

    def get_ranking_list(self):
        raise NotImplementedError()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not self.object.can_see_own_scoreboard(self.request.user):
            raise Http404()

        users, problems = self.get_ranking_list()
        context['users'] = users
        context['problems'] = problems
        context['last_msg'] = event.last()
        context['tab'] = self.tab
        return context


class ContestRanking(ContestRankingBase):
    tab = 'ranking'

    def get_title(self):
        return _('%s Rankings') % self.object.name

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user):
            queryset = self.object.users.filter(user=self.request.profile, virtual=ContestParticipation.LIVE)
            return get_contest_ranking_list(
                self.request, self.object,
                ranking_list=partial(base_contest_ranking_list, queryset=queryset),
                ranker=lambda users, key: ((_('???'), user) for user in users),
            )

        return get_contest_ranking_list(self.request, self.object)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = self.object.ratings.exists()
        return context


class ContestParticipationList(LoginRequiredMixin, ContestRankingBase):
    tab = 'participation'

    def get_title(self):
        if self.profile == self.request.profile:
            return _('Your participation in %(contest)s') % {'contest': self.object.name}
        return _("%(user)s's participation in %(contest)s") % {
            'user': self.profile.username, 'contest': self.object.name,
        }

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user) and self.profile != self.request.profile:
            raise Http404()

        queryset = self.object.users.filter(user=self.profile, virtual__gte=0).order_by('-virtual')
        live_link = format_html('<a href="{2}#!{1}">{0}</a>', _('Live'), self.profile.username,
                                reverse('contest_ranking', args=[self.object.key]))

        return get_contest_ranking_list(
            self.request, self.object, show_current_virtual=False,
            ranking_list=partial(base_contest_ranking_list, queryset=queryset),
            ranker=lambda users, key: ((user.participation.virtual or live_link, user) for user in users))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = False
        context['now'] = timezone.now()
        context['rank_header'] = _('Participation')
        return context

    def get(self, request, *args, **kwargs):
        if 'user' in kwargs:
            self.profile = get_object_or_404(Profile, user__username=kwargs['user'])
        else:
            self.profile = self.request.profile
        return super().get(request, *args, **kwargs)


class ContestParticipationDisqualify(ContestMixin, SingleObjectMixin, View):
    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        try:
            participation = self.object.users.get(pk=request.POST.get('participation'))
        except ObjectDoesNotExist:
            pass
        else:
            participation.set_disqualified(not participation.is_disqualified)
        return HttpResponseRedirect(reverse('contest_ranking', args=(self.object.key,)))


class ContestMossMixin(ContestMixin, PermissionRequiredMixin):
    permission_required = 'judge.moss_contest'

    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if settings.MOSS_API_KEY is None or not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest


class ContestMossView(ContestMossMixin, TitleMixin, DetailView):
    template_name = 'contest/moss.html'

    def get_title(self):
        return _('%s MOSS Results') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        problems = list(map(attrgetter('problem'), self.object.contest_problems.order_by('order')
                                                              .select_related('problem')))
        languages = list(map(itemgetter(0), ContestMoss.LANG_MAPPING))

        results = ContestMoss.objects.filter(contest=self.object)
        moss_results = defaultdict(list)
        for result in results:
            moss_results[result.problem].append(result)

        for result_list in moss_results.values():
            result_list.sort(key=lambda x: languages.index(x.language))

        context['languages'] = languages
        context['has_results'] = results.exists()
        context['moss_results'] = [(problem, moss_results[problem]) for problem in problems]

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        status = run_moss.delay(self.object.key)
        return redirect_to_task_status(
            status, message=_('Running MOSS for %s...') % (self.object.name,),
            redirect=reverse('contest_moss', args=(self.object.key,)),
        )


class ContestMossDelete(ContestMossMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        ContestMoss.objects.filter(contest=self.object).delete()
        return HttpResponseRedirect(reverse('contest_moss', args=(self.object.key,)))


class ContestTagDetailAjax(DetailView):
    model = ContestTag
    slug_field = slug_url_kwarg = 'name'
    context_object_name = 'tag'
    template_name = 'contest/tag-ajax.html'


class ContestTagDetail(TitleMixin, ContestTagDetailAjax):
    template_name = 'contest/tag.html'

    def get_title(self):
        return _('Contest tag: %s') % self.object.name


class ContestCreateView(TitleMixin, View):
    title = gettext_lazy("Create New Contest")

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect(reverse('auth_login') + '?next=' + request.path)

        org_id = request.GET.get('org') or request.POST.get('organization_id')
        self.selected_org = None
        if org_id:
            from judge.models import Organization
            try:
                self.selected_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                self.selected_org = None

        is_org_admin = (self.selected_org and hasattr(request, 'profile') and (
            self.selected_org.admins.filter(id=request.profile.id).exists() or
            self.selected_org.members.filter(id=request.profile.id).exists()
        ))

        if not (request.user.has_perm('judge.add_contest') or
                request.user.has_perm('judge.edit_all_contest') or
                request.user.has_perm('judge.edit_own_contest') or
                request.user.is_staff or request.user.is_superuser or is_org_admin):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        from judge import contest_format
        from judge.models import Problem, ContestTag
        formats = contest_format.choices()
        all_problems = Problem.objects.values('code', 'name', 'points').order_by('code')
        all_tags = ContestTag.objects.all().order_by('name')
        now = timezone.now()
        start_default = now + timedelta(hours=1)
        end_default = start_default + timedelta(hours=3)

        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        return render(request, "contest/create.html", {
            "title": self.get_title(),
            "now": now,
            "formats": formats,
            "all_problems": all_problems,
            "all_tags": all_tags,
            "start_default": start_default.strftime('%Y-%m-%dT%H:%M'),
            "end_default": end_default.strftime('%Y-%m-%dT%H:%M'),
            "selected_org": self.selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        from judge import contest_format
        from judge.models import Problem, ContestProblem, ContestTag
        import re
        import logging
        logger = logging.getLogger('judge.contests')

        formats = contest_format.choices()
        all_problems = Problem.objects.values('code', 'name', 'points').order_by('code')
        all_tags = ContestTag.objects.all().order_by('name')

        raw_key = request.POST.get('key', '').strip()
        clean_key = re.sub(r'[^a-zA-Z0-9_]', '', raw_key).lower()
        name = request.POST.get('name', '').strip()
        summary = request.POST.get('summary', '').strip()
        description = request.POST.get('description', '').strip()
        start_time_str = request.POST.get('start_time', '').strip()
        end_time_str = request.POST.get('end_time', '').strip()
        time_limit_str = request.POST.get('time_limit', '').strip()
        format_name = request.POST.get('format_name', 'default').strip()
        scoreboard_visibility = request.POST.get('scoreboard_visibility', 'V').strip()
        is_visible = bool(request.POST.get('is_visible'))
        is_rated = bool(request.POST.get('is_rated'))
        use_clarifications = bool(request.POST.get('use_clarifications'))
        hide_problem_tags = bool(request.POST.get('hide_problem_tags'))
        hide_problem_authors = bool(request.POST.get('hide_problem_authors'))
        access_code = request.POST.get('access_code', '').strip()

        org_id = request.POST.get('organization_id') or request.GET.get('org')
        target_org = self.selected_org
        if not target_org and org_id:
            from judge.models import Organization
            try:
                target_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                target_org = None

        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') and request.profile else []

        def render_error(err_msg):
            now = timezone.now()
            start_default = now + timedelta(hours=1)
            end_default = start_default + timedelta(hours=3)
            return render(request, "contest/create.html", {
                "title": self.get_title(),
                "error": err_msg,
                "formats": formats,
                "all_problems": all_problems,
                "all_tags": all_tags,
                "selected_org": target_org or self.selected_org,
                "user_orgs": user_orgs,
                "post": request.POST,
                "now": now,
                "start_default": start_default.strftime('%Y-%m-%dT%H:%M'),
                "end_default": end_default.strftime('%Y-%m-%dT%H:%M'),
            })

        # Validation
        if not clean_key:
            return render_error(_("Mã cuộc thi (ID) không hợp lệ. Vui lòng chỉ dùng chữ cái, số và dấu gạch dưới."))

        if len(clean_key) > 64:
            return render_error(_("Mã cuộc thi không được vượt quá 64 ký tự."))

        if Contest.objects.filter(key=clean_key).exists():
            return render_error(_("Mã cuộc thi '%s' đã tồn tại. Vui lòng chọn mã khác.") % clean_key)

        if not name:
            return render_error(_("Vui lòng nhập tên cuộc thi."))

        # Parse start_time & end_time
        try:
            start_time = datetime.datetime.fromisoformat(start_time_str.replace(' ', 'T'))
            if timezone.is_naive(start_time):
                start_time = timezone.make_aware(start_time)
        except Exception:
            return render_error(_("Thời gian bắt đầu không hợp lệ."))

        try:
            end_time = datetime.datetime.fromisoformat(end_time_str.replace(' ', 'T'))
            if timezone.is_naive(end_time):
                end_time = timezone.make_aware(end_time)
        except Exception:
            return render_error(_("Thời gian kết thúc không hợp lệ."))

        if end_time <= start_time:
            return render_error(_("Thời gian kết thúc phải diễn ra sau thời gian bắt đầu."))

        # Duration limit
        time_limit = None
        if time_limit_str:
            try:
                parts = time_limit_str.split(':')
                if len(parts) == 3:
                    time_limit = timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
                elif len(parts) == 2:
                    time_limit = timedelta(hours=int(parts[0]), minutes=int(parts[1]))
                elif len(parts) == 1 and parts[0].isdigit():
                    time_limit = timedelta(minutes=int(parts[0]))
            except Exception:
                time_limit = None

        try:
            with revisions.create_revision(atomic=True):
                contest = Contest.objects.create(
                    key=clean_key,
                    name=name,
                    summary=summary,
                    description=description,
                    start_time=start_time,
                    end_time=end_time,
                    time_limit=time_limit,
                    format_name=format_name,
                    scoreboard_visibility=scoreboard_visibility,
                    is_visible=is_visible,
                    is_rated=is_rated,
                    use_clarifications=use_clarifications,
                    hide_problem_tags=hide_problem_tags,
                    hide_problem_authors=hide_problem_authors,
                    access_code=access_code,
                    is_organization_private=bool(target_org),
                )
                if hasattr(request, 'profile') and request.profile:
                    contest.authors.add(request.profile)
                if target_org:
                    contest.organizations.add(target_org)

                # Selected tags
                selected_tag_ids = request.POST.getlist('tags')
                if selected_tag_ids:
                    contest.tags.set(ContestTag.objects.filter(id__in=selected_tag_ids))

                # Add problems with deduplication & integer points
                prob_codes = request.POST.getlist('problem_code[]')
                if not prob_codes:
                    prob_codes = request.POST.getlist('problem_codes')
                prob_points = request.POST.getlist('problem_points[]')

                seen_problems = set()
                order_idx = 1
                for idx, code in enumerate(prob_codes):
                    code = code.strip()
                    if not code or code in seen_problems:
                        continue
                    try:
                        prob_obj = Problem.objects.get(code=code)
                        if prob_obj.id in seen_problems:
                            continue
                        seen_problems.add(prob_obj.id)
                        seen_problems.add(code)

                        pts = None
                        if idx < len(prob_points) and prob_points[idx].strip():
                            try:
                                pts = int(round(float(prob_points[idx].strip())))
                            except (ValueError, TypeError):
                                pts = None

                        final_pts = pts if pts is not None else (int(round(prob_obj.points)) if prob_obj.points is not None else 100)

                        ContestProblem.objects.create(
                            contest=contest,
                            problem=prob_obj,
                            order=order_idx,
                            points=final_pts,
                            partial=True,
                        )
                        order_idx += 1
                    except Problem.DoesNotExist:
                        continue

                revisions.set_user(request.user)
                revisions.set_comment(_("Created contest via custom add contest form"))

            return HttpResponseRedirect(reverse('contest_view', args=[clean_key]))
        except Exception as e:
            logger.exception("Failed to create contest: %s", e)
            return render_error(_("Không thể tạo kỳ thi: %s") % str(e))



class ContestEditView(TitleMixin, View):
    title = gettext_lazy("Edit Contest")

    def dispatch(self, request, contest, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect(reverse('auth_login') + '?next=' + request.path)

        from judge.models import Contest
        self.contest = get_object_or_404(
            Contest.objects.prefetch_related('organizations', 'tags', 'authors', 'curators'),
            key=contest
        )

        is_org_admin = (
            hasattr(request, 'profile') and request.profile and
            self.contest.organizations.filter(admins=request.profile).exists()
        )

        can_edit = (
            self.contest.is_editable_by(request.user) or
            request.user.is_staff or
            request.user.is_superuser or
            is_org_admin
        )

        if not can_edit:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied()

        return super().dispatch(request, contest, *args, **kwargs)

    def get_title(self):
        return _("Chỉnh sửa kỳ thi: %(name)s") % {'name': self.contest.name}

    def get(self, request, contest):
        from judge import contest_format
        from judge.models import Problem, ContestTag
        formats = contest_format.choices()
        all_problems = Problem.objects.values('code', 'name', 'points').order_by('code')
        all_tags = ContestTag.objects.all().order_by('name')

        contest_problems = self.contest.contest_problems.all().select_related('problem').order_by('order')

        # Formatted start / end times in user's or current timezone
        tz = timezone.get_current_timezone()
        start_val = self.contest.start_time.astimezone(tz).strftime('%Y-%m-%dT%H:%M') if self.contest.start_time else ''
        end_val = self.contest.end_time.astimezone(tz).strftime('%Y-%m-%dT%H:%M') if self.contest.end_time else ''

        # Time limit formatted
        time_limit_val = ''
        if self.contest.time_limit:
            total_sec = int(self.contest.time_limit.total_seconds())
            h = total_sec // 3600
            m = (total_sec % 3600) // 60
            s = total_sec % 60
            if s > 0:
                time_limit_val = f'{h:02d}:{m:02d}:{s:02d}'
            else:
                time_limit_val = f'{h:02d}:{m:02d}'

        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') and request.profile else []
        selected_org_ids = list(self.contest.organizations.values_list('id', flat=True))
        selected_tag_ids = list(self.contest.tags.values_list('id', flat=True))

        return render(request, "contest/edit.html", {
            "title": self.get_title(),
            "contest": self.contest,
            "formats": formats,
            "all_problems": all_problems,
            "all_tags": all_tags,
            "contest_problems": contest_problems,
            "start_val": start_val,
            "end_val": end_val,
            "time_limit_val": time_limit_val,
            "user_orgs": user_orgs,
            "selected_org_ids": selected_org_ids,
            "selected_tag_ids": selected_tag_ids,
            "can_edit": True,
        })

    def post(self, request, contest):
        from judge import contest_format
        from judge.models import Problem, ContestProblem, ContestTag, Organization
        import logging
        logger = logging.getLogger('judge.contests')

        formats = contest_format.choices()
        all_problems = Problem.objects.values('code', 'name', 'points').order_by('code')
        all_tags = ContestTag.objects.all().order_by('name')
        contest_problems = self.contest.contest_problems.all().select_related('problem').order_by('order')

        name = request.POST.get('name', '').strip()
        summary = request.POST.get('summary', '').strip()
        description = request.POST.get('description', '').strip()
        start_time_str = request.POST.get('start_time', '').strip()
        end_time_str = request.POST.get('end_time', '').strip()
        time_limit_str = request.POST.get('time_limit', '').strip()
        format_name = request.POST.get('format_name', 'default').strip()
        scoreboard_visibility = request.POST.get('scoreboard_visibility', 'V').strip()
        is_visible = bool(request.POST.get('is_visible'))
        is_rated = bool(request.POST.get('is_rated'))
        use_clarifications = bool(request.POST.get('use_clarifications'))
        hide_problem_tags = bool(request.POST.get('hide_problem_tags'))
        hide_problem_authors = bool(request.POST.get('hide_problem_authors'))
        access_code = request.POST.get('access_code', '').strip()

        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') and request.profile else []
        selected_org_ids = list(self.contest.organizations.values_list('id', flat=True))
        selected_tag_ids = list(self.contest.tags.values_list('id', flat=True))

        tz = timezone.get_current_timezone()
        start_val = self.contest.start_time.astimezone(tz).strftime('%Y-%m-%dT%H:%M') if self.contest.start_time else ''
        end_val = self.contest.end_time.astimezone(tz).strftime('%Y-%m-%dT%H:%M') if self.contest.end_time else ''
        time_limit_val = time_limit_str

        def render_error(err_msg):
            return render(request, "contest/edit.html", {
                "title": self.get_title(),
                "contest": self.contest,
                "error": err_msg,
                "formats": formats,
                "all_problems": all_problems,
                "all_tags": all_tags,
                "contest_problems": contest_problems,
                "start_val": start_time_str or start_val,
                "end_val": end_time_str or end_val,
                "time_limit_val": time_limit_val,
                "user_orgs": user_orgs,
                "selected_org_ids": selected_org_ids,
                "selected_tag_ids": selected_tag_ids,
                "post": request.POST,
                "can_edit": True,
            })

        if not name:
            return render_error(_("Vui lòng nhập tên cuộc thi."))

        # Parse start_time & end_time
        try:
            start_time = datetime.datetime.fromisoformat(start_time_str.replace(' ', 'T'))
            if timezone.is_naive(start_time):
                start_time = timezone.make_aware(start_time)
        except Exception:
            return render_error(_("Thời gian bắt đầu không hợp lệ."))

        try:
            end_time = datetime.datetime.fromisoformat(end_time_str.replace(' ', 'T'))
            if timezone.is_naive(end_time):
                end_time = timezone.make_aware(end_time)
        except Exception:
            return render_error(_("Thời gian kết thúc không hợp lệ."))

        if end_time <= start_time:
            return render_error(_("Thời gian kết thúc phải diễn ra sau thời gian bắt đầu."))

        # Duration limit
        time_limit = None
        if time_limit_str:
            try:
                parts = time_limit_str.split(':')
                if len(parts) == 3:
                    time_limit = timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
                elif len(parts) == 2:
                    time_limit = timedelta(hours=int(parts[0]), minutes=int(parts[1]))
                elif len(parts) == 1 and parts[0].isdigit():
                    time_limit = timedelta(minutes=int(parts[0]))
            except Exception:
                time_limit = None

        try:
            with revisions.create_revision(atomic=True):
                self.contest.name = name
                self.contest.summary = summary
                self.contest.description = description
                self.contest.start_time = start_time
                self.contest.end_time = end_time
                self.contest.time_limit = time_limit
                self.contest.format_name = format_name
                self.contest.scoreboard_visibility = scoreboard_visibility
                self.contest.is_visible = is_visible
                self.contest.is_rated = is_rated
                self.contest.use_clarifications = use_clarifications
                self.contest.hide_problem_tags = hide_problem_tags
                self.contest.hide_problem_authors = hide_problem_authors
                self.contest.access_code = access_code

                # Update organization if provided
                org_id = request.POST.get('organization_id')
                if org_id is not None:
                    if org_id.strip() == '':
                        self.contest.organizations.clear()
                        self.contest.is_organization_private = False
                    else:
                        try:
                            org_obj = Organization.objects.get(id=int(org_id))
                            self.contest.organizations.set([org_obj])
                            self.contest.is_organization_private = True
                        except (Organization.DoesNotExist, ValueError):
                            pass

                self.contest.save()

                # Update tags
                selected_tags = request.POST.getlist('tags')
                if selected_tags:
                    self.contest.tags.set(ContestTag.objects.filter(id__in=selected_tags))
                else:
                    self.contest.tags.clear()

                # Update problems
                prob_codes = request.POST.getlist('problem_code[]')
                if not prob_codes:
                    prob_codes = request.POST.getlist('problem_codes')
                prob_points = request.POST.getlist('problem_points[]')

                seen_problems = set()
                kept_cp_ids = set()
                order_idx = 1

                for idx, code in enumerate(prob_codes):
                    code = code.strip()
                    if not code or code in seen_problems:
                        continue
                    try:
                        prob_obj = Problem.objects.get(code=code)
                        if prob_obj.id in seen_problems:
                            continue
                        seen_problems.add(prob_obj.id)
                        seen_problems.add(code)

                        pts = None
                        if idx < len(prob_points) and prob_points[idx].strip():
                            try:
                                pts = int(round(float(prob_points[idx].strip())))
                            except (ValueError, TypeError):
                                pts = None
                        final_pts = pts if pts is not None else (int(round(prob_obj.points)) if prob_obj.points is not None else 100)

                        cp, created = ContestProblem.objects.update_or_create(
                            contest=self.contest,
                            problem=prob_obj,
                            defaults={
                                'order': order_idx,
                                'points': final_pts,
                                'partial': True,
                            }
                        )
                        kept_cp_ids.add(cp.id)
                        order_idx += 1
                    except Problem.DoesNotExist:
                        continue

                # Delete removed contest problems
                self.contest.contest_problems.exclude(id__in=kept_cp_ids).delete()

                # Recompute results for all participations in this contest
                for part in self.contest.users.all():
                    part.recompute_results()

                revisions.set_user(request.user)
                revisions.set_comment(_("Edited contest via custom edit contest form"))

            return HttpResponseRedirect(reverse('contest_view', args=[self.contest.key]))
        except Exception as e:
            logger.exception("Failed to update contest: %s", e)
            return render_error(_("Không thể cập nhật kỳ thi: %s") % str(e))
