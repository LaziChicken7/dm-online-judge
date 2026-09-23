from django import forms
from django.urls import reverse_lazy
from judge.widgets import MartorWidget, Select2MultipleWidget, Select2Widget
import shutil
import logging
import os
import re
from datetime import timedelta
from operator import itemgetter
from random import randrange
from statistics import mean, median

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.db.models import BooleanField, Case, CharField, Count, F, FilteredRelation, Prefetch, Q, When
from django.db.models.functions import Coalesce
from django.db.utils import ProgrammingError
from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.functional import cached_property
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import DetailView, ListView, View
from django.views.generic.detail import SingleObjectMixin
from reversion import revisions

from judge.comments import CommentedDetailView
from judge.forms import ProblemCloneForm, ProblemPointsVoteForm, ProblemSubmitForm
from judge.models import ContestSubmission, Judge, Language, Problem, ProblemGroup, ProblemPointsVote, \
    ProblemTranslation, ProblemType, Profile, RuntimeVersion, Solution, Submission, SubmissionSource
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.opengraph import generate_opengraph
from judge.utils.pdfoid import PDF_RENDERING_ENABLED, render_pdf
from judge.utils.problems import contest_attempted_ids, contest_completed_ids, hot_problems, user_attempted_ids, \
    user_completed_ids
from judge.utils.strings import safe_float_or_none, safe_int_or_none
from judge.utils.tickets import own_ticket_filter
from judge.utils.views import QueryStringSortMixin, SingleObjectFormView, TitleMixin, add_file_response, generic_message

recjk = re.compile(r'[\u2E80-\u2E99\u2E9B-\u2EF3\u2F00-\u2FD5\u3005\u3007\u3021-\u3029\u3038-\u303A\u303B\u3400-\u4DB5'
                   r'\u4E00-\u9FC3\uF900-\uFA2D\uFA30-\uFA6A\uFA70-\uFAD9\U00020000-\U0002A6D6\U0002F800-\U0002FA1D]')


def get_contest_problem(problem, profile):
    try:
        return problem.contests.get(contest_id=profile.current_contest.contest_id)
    except ObjectDoesNotExist:
        return None


def get_contest_submission_count(problem, profile, virtual):
    return profile.current_contest.submissions.exclude(submission__status__in=['IE']) \
                  .filter(problem__problem=problem, participation__virtual=virtual).count()


class ProblemMixin(object):
    model = Problem
    slug_url_kwarg = 'problem'
    slug_field = 'code'

    def get_object(self, queryset=None):
        problem = super(ProblemMixin, self).get_object(queryset)
        if not problem.is_accessible_by(self.request.user):
            raise Http404()
        return problem

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(self.request, _('No such problem'),
                               _('Could not find a problem with the code "%s".') % code, status=404)

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404:
            return self.no_such_problem()


class SolvedProblemMixin(object):
    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        if self.in_contest:
            return contest_attempted_ids(self.profile.current_contest)
        else:
            return user_attempted_ids(self.profile) if self.profile is not None else ()

    @cached_property
    def in_contest(self):
        return self.profile is not None and self.profile.current_contest is not None

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile


class ProblemSolution(SolvedProblemMixin, ProblemMixin, TitleMixin, CommentedDetailView):
    context_object_name = 'problem'
    template_name = 'problem/editorial.html'

    def get_title(self):
        return _('Editorial for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Editorial for {0}')).format(
            format_html('<a href="{1}">{0}</a>', self.object.name, reverse('problem_detail', args=[self.object.code])),
        ))

    def get_context_data(self, **kwargs):
        context = super(ProblemSolution, self).get_context_data(**kwargs)

        solution = get_object_or_404(Solution, problem=self.object)

        if not solution.is_accessible_by(self.request.user) or self.request.in_contest:
            raise Http404()
        context['solution'] = solution
        context['has_solved_problem'] = self.object.id in self.get_completed_problems()
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS
        return context

    def get_comment_page(self):
        return 's:' + self.object.code

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(self.request, _('No such editorial'),
                               _('Could not find an editorial with the code "%s".') % code, status=404)


class ProblemDetail(ProblemMixin, SolvedProblemMixin, CommentedDetailView):
    context_object_name = 'problem'
    template_name = 'problem/problem.html'

    def get_comment_page(self):
        return 'p:%s' % self.object.code

    def get_context_data(self, **kwargs):
        context = super(ProblemDetail, self).get_context_data(**kwargs)
        user = self.request.user
        authed = user.is_authenticated
        context['has_submissions'] = authed and Submission.objects.filter(user=user.profile,
                                                                          problem=self.object).exists()
        contest_problem = (None if not authed or user.profile.current_contest is None else
                           get_contest_problem(self.object, user.profile))
        context['contest_problem'] = contest_problem
        if contest_problem:
            clarifications = self.object.clarifications
            context['has_clarifications'] = clarifications.count() > 0
            context['clarifications'] = clarifications.order_by('-date')
            context['submission_limit'] = contest_problem.max_submissions
            if contest_problem.max_submissions:
                context['submissions_left'] = max(contest_problem.max_submissions -
                                                  get_contest_submission_count(self.object, user.profile,
                                                                               user.profile.current_contest.virtual), 0)

        if self.object.is_vjudge:
            context['available_judges_display'] = f"VJudge ({self.object.vjudge_oj or 'Remote'})"
        elif getattr(self.object, 'is_clue', False):
            if getattr(self.object, 'is_clue_org', False) or getattr(self.object, 'clue_organization', ''):
                context['available_judges_display'] = "ClueOJ Org (Local & Remote)"
            else:
                context['available_judges_display'] = "ClueOJ (Remote)"
        elif getattr(self.object, 'is_ntucoder', False):
            context['available_judges_display'] = "NTUCoder (Remote)"
        elif getattr(self.object, 'is_ltpt', False):
            context['available_judges_display'] = "LTPT (Remote)"
        else:
            judges = Judge.objects.filter(online=True, problems=self.object)
            if not judges.exists():
                judges = Judge.objects.filter(online=True)
            context['available_judges'] = judges
        context['show_languages'] = self.object.allowed_languages.count() != Language.objects.count()
        context['has_pdf_render'] = PDF_RENDERING_ENABLED
        context['completed_problem_ids'] = self.get_completed_problems()
        context['attempted_problems'] = self.get_attempted_problems()

        can_edit = self.object.is_editable_by(user)
        context['can_edit_problem'] = can_edit
        if user.is_authenticated:
            tickets = self.object.tickets
            if not can_edit:
                tickets = tickets.filter(own_ticket_filter(user.profile.id))
            context['has_tickets'] = tickets.exists()
            context['num_open_tickets'] = tickets.filter(is_open=True).values('id').distinct().count()

        try:
            context['editorial'] = Solution.objects.get(problem=self.object)
        except ObjectDoesNotExist:
            pass
        try:
            translation = self.object.translations.get(language=self.request.LANGUAGE_CODE)
        except ProblemTranslation.DoesNotExist:
            context['title'] = self.object.name
            context['language'] = settings.LANGUAGE_CODE
            context['description'] = self.object.description
            context['translated'] = False
        else:
            context['title'] = translation.name
            context['language'] = self.request.LANGUAGE_CODE
            context['description'] = translation.description
            context['translated'] = True

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph('generated-meta-problem:%s:%d' % (context['language'], self.object.id),
                                          context['description'], 'problem')
        context['meta_description'] = self.object.summary or metadata[0]
        context['og_image'] = self.object.og_image or metadata[1]
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS

        context['vote_perm'] = self.object.vote_permission_for_user(user)
        if context['vote_perm'].can_vote():
            try:
                context['vote'] = ProblemPointsVote.objects.get(voter=user.profile, problem=self.object)
            except ObjectDoesNotExist:
                context['vote'] = None
        else:
            context['vote'] = None

        if self.object.is_vjudge:
            from judge.utils.vjudge_service import (
                get_vjudge_problem_data,
                get_vjudge_statement_content,
                select_best_vjudge_statement,
            )
            from django.utils.translation import get_language

            cookie = self.request.profile.vjudge_cookie if authed else None
            data = get_vjudge_problem_data(self.object.vjudge_oj, self.object.vjudge_prob_num, cookie=cookie)
            statements = data.get('descBriefs', []) if data else []
            context['vjudge_statements'] = statements

            user_lang = (
                self.request.GET.get('lang') or
                getattr(self.request, 'LANGUAGE_CODE', None) or
                get_language() or
                'en'
            )
            requested_key = self.request.GET.get('stmt') or self.request.GET.get('key')
            best_stmt = select_best_vjudge_statement(statements, user_lang=user_lang, requested_key=requested_key)
            default_key = str(best_stmt['key']) if best_stmt and best_stmt.get('key') else None
            context['vjudge_default_key'] = default_key

            if best_stmt:
                context['vjudge_active_author'] = best_stmt.get('author') or 'System'
                context['vjudge_active_lang'] = best_stmt.get('langDisplay') or best_stmt.get('lang') or 'English'
            else:
                context['vjudge_active_author'] = 'System'
                context['vjudge_active_lang'] = 'English'

            if default_key:
                context['vjudge_statement_html'] = get_vjudge_statement_content(default_key, cookie=cookie)
            else:
                context['vjudge_statement_html'] = ""

        if getattr(self.object, 'is_clue', False):
            import re
            from judge.utils.vjudge_service import clean_vjudge_math
            context['is_clue'] = True
            desc = self.object.description or ""

            # Extract PDF URL
            pdf_m = re.search(r'<!--\s*CLUE_PDF:(https://[^\s>]+)\s*-->', desc)
            if pdf_m:
                context['clue_pdf_url'] = pdf_m.group(1)
            else:
                pdf_m = re.search(r'https://oj\.clue\.edu\.vn/pdf/[a-zA-Z0-9_-]+\.pdf', desc)
                if not pdf_m:
                    pdf_m = re.search(r'/pdf/[a-zA-Z0-9_-]+\.pdf', desc)
                    context['clue_pdf_url'] = f"https://oj.clue.edu.vn{pdf_m.group(0)}" if pdf_m else None
                else:
                    context['clue_pdf_url'] = pdf_m.group(0)

            clean_html = desc
            # Strip PDF comment marker if present
            clean_html = re.sub(r'<!--\s*CLUE_PDF:[^>]*-->\s*', '', clean_html)

            # Strip legacy markdown headers if present
            lines = clean_html.splitlines()
            body_lines = []
            skipping = True
            for l in lines:
                s = l.strip()
                if skipping:
                    if s.startswith('## ClueOJ') or s.startswith('*Đề bài được nhập') or s.startswith('[Tài liệu PDF gốc]') or s == '---' or not s:
                        continue
                    else:
                        skipping = False
                body_lines.append(l)

            clean_html = '\n'.join(body_lines)
            clean_html = re.sub(r'<iframe[\s\S]*?</iframe>', '', clean_html, flags=re.I)
            clean_html = re.sub(r'<object[\s\S]*?</object>', '', clean_html, flags=re.I)
            clean_html = re.sub(r'<embed[\s\S]*?>', '', clean_html, flags=re.I)
            clean_html = re.sub(r'<p>\s*Trong trường hợp đề bài hiển thị không chính xác[\s\S]*?</p>', '', clean_html, flags=re.I)
            clean_html = re.sub(r'href="/(.*?)"', r'href="https://oj.clue.edu.vn/\1"', clean_html)
            clean_html = re.sub(r'src="/(.*?)"', r'src="https://oj.clue.edu.vn/\1"', clean_html)

            clean_html = clean_vjudge_math(clean_html)
            context['clue_statement_html'] = clean_html
            context['clue_statement_html_vi'] = clean_html
            en_trans = self.object.translations.filter(language__startswith='en').first()
            if en_trans and en_trans.description:
                en_html = en_trans.description
                en_html = re.sub(r'<iframe[\s\S]*?</iframe>', '', en_html, flags=re.I)
                en_html = clean_vjudge_math(en_html)
                context['clue_statement_html_en'] = en_html

        if getattr(self.object, 'is_ntucoder', False) or getattr(self.object, 'is_ltpt', False):
            import re
            from judge.utils.ntucoder_service import clean_ntucoder_math, format_ntucoder_samples
            context['is_ntucoder'] = True
            context['ntucoder_id'] = self.object.ntucoder_id
            context['ntucoder_code'] = self.object.ntucoder_code
            desc = self.object.description or ""
            clean_html = re.sub(r'<!--\s*NTUCODER_ORIGIN:[^>]*-->\s*', '', desc)
            clean_html = format_ntucoder_samples(clean_ntucoder_math(clean_html))
            context['ntucoder_statement_html'] = clean_html

        if getattr(self.object, 'is_ltpt', False):
            import re
            from judge.utils.ltpt_service import clean_ltpt_math, format_ltpt_samples
            context['is_ltpt'] = True
            context['ltpt_code'] = self.object.ltpt_code
            desc = self.object.description or ""
            clean_html = re.sub(r'<!--\s*LTPT_ORIGIN:[^>]*-->\s*', '', desc)
            clean_html = format_ltpt_samples(clean_ltpt_math(clean_html))
            context['ltpt_statement_html'] = clean_html

        return context


class ProblemVote(ProblemMixin, DetailView):
    context_object_name = 'problem'
    template_name = 'problem/vote-ajax.html'

    def get_context_data(self, **kwargs):
        if not self.object.vote_permission_for_user(self.request.user).can_vote():
            raise Http404()

        context = super().get_context_data(**kwargs)

        try:
            context['vote'] = ProblemPointsVote.objects.get(voter=self.request.profile, problem=self.object)
        except ObjectDoesNotExist:
            context['vote'] = None

        context['max_possible_vote'] = settings.DMOJ_PROBLEM_MAX_USER_POINTS_VOTE
        context['min_possible_vote'] = settings.DMOJ_PROBLEM_MIN_USER_POINTS_VOTE
        return context

    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.vote_permission_for_user(request.user).can_vote():
            return JsonResponse({'message': _('Not allowed to vote on this problem.')}, status=403)

        form = ProblemPointsVoteForm(request.POST)
        if not form.is_valid():
            return JsonResponse(form.errors, status=400)

        with transaction.atomic():
            # Delete any pre-existing votes.
            ProblemPointsVote.objects.filter(voter=request.profile, problem=problem).delete()
            vote = form.save(commit=False)
            vote.voter = request.profile
            vote.problem = problem
            vote.save()

        return JsonResponse({'points': vote.points})


class DeleteProblemVote(ProblemMixin, SingleObjectMixin, View):
    http_method_names = ['options', 'post']  # This disables GET requests, even though ProblemMixin.get exists.

    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.vote_permission_for_user(request.user).can_vote():
            return JsonResponse({'message': _('Not allowed to delete votes on this problem.')}, status=403)

        ProblemPointsVote.objects.filter(voter=request.profile, problem=problem).delete()
        return JsonResponse({'message': _('success')})


class ProblemVoteStats(ProblemMixin, DetailView):
    context_object_name = 'problem'
    template_name = 'problem/vote-stats-ajax.html'

    def get_context_data(self, **kwargs):
        if not self.object.vote_permission_for_user(self.request.user).can_view():
            raise Http404()

        context = super().get_context_data(**kwargs)

        votes = list(self.object.problem_points_votes.order_by('points').values_list('points', flat=True))
        context['votes'] = votes

        if votes:
            context['mean'] = mean(votes)
            context['median'] = median(votes)

        context['max_possible_vote'] = settings.DMOJ_PROBLEM_MAX_USER_POINTS_VOTE
        context['min_possible_vote'] = settings.DMOJ_PROBLEM_MIN_USER_POINTS_VOTE
        return context


class LatexError(Exception):
    pass


class ProblemPdfView(ProblemMixin, SingleObjectMixin, View):
    logger = logging.getLogger('judge.problem.pdf')
    languages = set(map(itemgetter(0), settings.LANGUAGES))

    def get(self, request, *args, **kwargs):
        if not PDF_RENDERING_ENABLED:
            raise Http404()

        language = kwargs.get('language', self.request.LANGUAGE_CODE)
        if language not in self.languages:
            raise Http404()

        problem = self.get_object()
        pdf_basename = '%s.%s.pdf' % (problem.code, language)

        def render_problem_pdf():
            self.logger.info('Rendering PDF in %s: %s', language, problem.code)

            with translation.override(language):
                try:
                    trans = problem.translations.get(language=language)
                except ProblemTranslation.DoesNotExist:
                    trans = None

                problem_name = trans.name if trans else problem.name
                return render_pdf(
                    html=get_template('problem/raw.html').render({
                        'problem': problem,
                        'problem_name': problem_name,
                        'description': trans.description if trans else problem.description,
                        'url': request.build_absolute_uri(),
                    }).replace('"//', '"https://').replace("'//", "'https://"),
                    title=problem_name,
                )

        response = HttpResponse()
        response['Content-Type'] = 'application/pdf'
        response['Content-Disposition'] = f'inline; filename={pdf_basename}'

        if settings.DMOJ_PDF_PROBLEM_CACHE:
            pdf_filename = os.path.join(settings.DMOJ_PDF_PROBLEM_CACHE, pdf_basename)
            if not os.path.exists(pdf_filename):
                with open(pdf_filename, 'wb') as f:
                    f.write(render_problem_pdf())

            if settings.DMOJ_PDF_PROBLEM_INTERNAL:
                url_path = f'{settings.DMOJ_PDF_PROBLEM_INTERNAL}/{pdf_basename}'
            else:
                url_path = None

            add_file_response(request, response, url_path, pdf_filename)
        else:
            response.content = render_problem_pdf()

        return response


class ProblemList(QueryStringSortMixin, TitleMixin, SolvedProblemMixin, ListView):
    model = Problem
    title = gettext_lazy('Problems')
    context_object_name = 'problems'
    template_name = 'problem/list.html'
    paginate_by = 50

    def get_paginate_by(self, queryset):
        return 50
    sql_sort = frozenset(('points', 'ac_rate', 'user_count', 'code'))
    manual_sort = frozenset(('name', 'group', 'solved', 'type', 'editorial'))
    all_sorts = sql_sort | manual_sort
    default_desc = frozenset(('points', 'ac_rate', 'user_count'))
    default_sort = 'code'

    def get_paginator(self, queryset, per_page, orphans=0,
                      allow_empty_first_page=True, **kwargs):
        paginator = DiggPaginator(queryset, per_page, body=6, padding=2, orphans=orphans,
                                  count=queryset.values('pk').count() if not self.in_contest else None,
                                  allow_empty_first_page=allow_empty_first_page, **kwargs)
        if not self.in_contest:
            queryset = queryset.add_i18n_name(self.request.LANGUAGE_CODE)
            sort_key = self.order.lstrip('-')
            if sort_key in self.sql_sort:
                queryset = queryset.order_by(self.order, 'id')
            elif sort_key == 'name':
                queryset = queryset.order_by(self.order.replace('name', 'i18n_name'), 'id')
            elif sort_key == 'group':
                queryset = queryset.order_by(self.order + '__name', 'id')
            elif sort_key == 'editorial':
                queryset = queryset.order_by(self.order.replace('editorial', 'has_public_editorial'), 'id')
            elif sort_key == 'solved':
                if self.request.user.is_authenticated:
                    profile = self.request.profile
                    solved = user_completed_ids(profile)
                    attempted = user_attempted_ids(profile)

                    def _solved_sort_order(problem):
                        if problem.id in solved:
                            return 1
                        if problem.id in attempted:
                            return 0
                        return -1

                    queryset = list(queryset)
                    queryset.sort(key=_solved_sort_order, reverse=self.order.startswith('-'))
            elif sort_key == 'type':
                if self.show_types:
                    queryset = list(queryset)
                    queryset.sort(key=lambda problem: problem.types_list[0] if problem.types_list else '',
                                  reverse=self.order.startswith('-'))
            paginator.object_list = queryset
        return paginator

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    def get_contest_queryset(self):
        queryset = self.profile.current_contest.contest.contest_problems.select_related('problem__group') \
            .defer('problem__description').order_by('problem__code') \
            .annotate(user_count=Count('submission__participation', distinct=True)) \
            .annotate(i18n_translation=FilteredRelation(
                'problem__translations', condition=Q(problem__translations__language=self.request.LANGUAGE_CODE),
            )).annotate(i18n_name=Coalesce(
                F('i18n_translation__name'), F('problem__name'), output_field=CharField(),
            )).order_by('order')
        return [{
            'id': p['problem_id'],
            'code': p['problem__code'],
            'name': p['problem__name'],
            'i18n_name': p['i18n_name'],
            'group': {'full_name': p['problem__group__full_name']},
            'points': p['points'],
            'partial': p['partial'],
            'user_count': p['user_count'],
        } for p in queryset.values('problem_id', 'problem__code', 'problem__name', 'i18n_name',
                                   'problem__group__full_name', 'points', 'partial', 'user_count')]

    @staticmethod
    def apply_full_text(queryset, query):
        if recjk.search(query):
            # MariaDB can't tokenize CJK properly, fallback to LIKE '%term%' for each term.
            for term in query.split():
                queryset = queryset.filter(Q(code__icontains=term) | Q(name__icontains=term) |
                                           Q(description__icontains=term))
            return queryset
        return queryset.search(query, queryset.BOOLEAN).extra(order_by=['-relevance'])

    def get_normal_queryset(self):
        filter = Q(is_public=True)
        if not self.request.user.has_perm('see_organization_problem'):
            org_filter = Q(is_organization_private=False)
            if self.profile is not None:
                org_filter |= Q(organizations__in=self.profile.organizations.all())
            filter &= org_filter
        if self.profile is not None:
            filter = Problem.q_add_author_curator_tester(filter, self.profile)
        queryset = Problem.objects.filter(filter).select_related('group').defer('description', 'summary')
        if self.profile is not None and self.hide_solved:
            queryset = queryset.exclude(id__in=Submission.objects
                                        .filter(user=self.profile, is_archived=False,
                                                result='AC', case_points__gte=F('case_total'))
                                        .values_list('problem_id', flat=True))
        if self.show_types:
            queryset = queryset.prefetch_related('types')
        queryset = queryset.annotate(has_public_editorial=Case(
            When(solution__is_public=True, solution__publish_on__lte=timezone.now(), then=True),
            default=False,
            output_field=BooleanField(),
        ))
        if self.has_public_editorial:
            queryset = queryset.filter(has_public_editorial=True)
        if self.category is not None:
            queryset = queryset.filter(group__id=self.category)
        if self.selected_types:
            queryset = queryset.filter(types__in=self.selected_types)
        if 'search' in self.request.GET:
            self.search_query = query = ' '.join(self.request.GET.getlist('search')).strip()
            if query:
                if settings.ENABLE_FTS and self.full_text:
                    queryset = self.apply_full_text(queryset, query)
                else:
                    queryset = queryset.filter(
                        Q(code__icontains=query) | Q(name__icontains=query) |
                        Q(translations__name__icontains=query, translations__language=self.request.LANGUAGE_CODE))
        self.prepoint_queryset = queryset
        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)
        return queryset.distinct()

    def get_queryset(self):
        if self.in_contest:
            return self.get_contest_queryset()
        else:
            return self.get_normal_queryset()

    def get_context_data(self, **kwargs):
        context = super(ProblemList, self).get_context_data(**kwargs)
        context['hide_solved'] = 0 if self.in_contest else int(self.hide_solved)
        context['show_types'] = 0 if self.in_contest else int(self.show_types)
        context['has_public_editorial'] = 0 if self.in_contest else int(self.has_public_editorial)
        context['full_text'] = 0 if self.in_contest else int(self.full_text)
        context['category'] = self.category
        context['categories'] = ProblemGroup.objects.all()
        if self.show_types:
            context['selected_types'] = self.selected_types
            context['problem_types'] = ProblemType.objects.all()
        context['has_fts'] = settings.ENABLE_FTS
        context['search_query'] = self.search_query
        context['completed_problem_ids'] = self.get_completed_problems()
        context['attempted_problems'] = self.get_attempted_problems()

        context.update(self.get_sort_paginate_context())
        if not self.in_contest:
            context.update(self.get_sort_context())
            context['hot_problems'] = hot_problems(timedelta(days=1), settings.DMOJ_PROBLEM_HOT_PROBLEM_COUNT)
            context['point_start'], context['point_end'], context['point_values'] = self.get_noui_slider_points()
        else:
            context['hot_problems'] = None
            context['point_start'], context['point_end'], context['point_values'] = 0, 0, {}
            context['hide_contest_scoreboard'] = self.contest.scoreboard_visibility in (
                self.contest.SCOREBOARD_AFTER_CONTEST,
                self.contest.SCOREBOARD_AFTER_PARTICIPATION,
                self.contest.SCOREBOARD_HIDDEN,
            )
        return context

    def get_noui_slider_points(self):
        points = sorted(self.prepoint_queryset.values_list('points', flat=True).distinct())
        if not points:
            return 0, 0, {}
        if len(points) == 1:
            return points[0] - 1, points[0] + 1, {
                'min': points[0] - 1,
                '50%': points[0],
                'max': points[0] + 1,
            }

        start, end = points[0], points[-1]
        if self.point_start is not None:
            start = self.point_start
        if self.point_end is not None:
            end = self.point_end
        points_map = {0.0: 'min', 1.0: 'max'}
        size = len(points) - 1
        return start, end, {points_map.get(i / size, '%.2f%%' % (100 * i / size,)): j for i, j in enumerate(points)}

    def GET_with_session(self, request, key):
        if not request.GET:
            return request.session.get(key, False)
        return request.GET.get(key, None) == '1'

    def setup_problem_list(self, request):
        self.hide_solved = self.GET_with_session(request, 'hide_solved')
        self.show_types = self.GET_with_session(request, 'show_types')
        self.full_text = self.GET_with_session(request, 'full_text')
        self.has_public_editorial = self.GET_with_session(request, 'has_public_editorial')

        self.search_query = None
        self.category = None
        self.selected_types = []

        # This actually copies into the instance dictionary...
        self.all_sorts = set(self.all_sorts)
        if not self.show_types:
            self.all_sorts.discard('type')

        self.category = safe_int_or_none(request.GET.get('category'))
        if 'type' in request.GET:
            try:
                self.selected_types = list(map(int, request.GET.getlist('type')))
            except ValueError:
                pass

        self.point_start = safe_float_or_none(request.GET.get('point_start'))
        self.point_end = safe_float_or_none(request.GET.get('point_end'))

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)

        try:
            return super(ProblemList, self).get(request, *args, **kwargs)
        except ProgrammingError as e:
            return generic_message(request, 'FTS syntax error', e.args[1], status=400)

    def post(self, request, *args, **kwargs):
        to_update = ('hide_solved', 'show_types', 'has_public_editorial', 'full_text')
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == '1'
                request.session[key] = val
            else:
                request.session.pop(key, None)
        return HttpResponseRedirect(request.get_full_path())


class LanguageTemplateAjax(View):
    def get(self, request, *args, **kwargs):
        try:
            language = get_object_or_404(Language, id=int(request.GET.get('id', 0)))
        except ValueError:
            raise Http404()
        return HttpResponse(language.template, content_type='text/plain')


class RandomProblem(ProblemList):
    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)
        if self.in_contest:
            raise Http404()

        queryset = self.get_normal_queryset()
        count = queryset.count()
        if not count:
            return HttpResponseRedirect('%s%s%s' % (reverse('problem_list'), request.META['QUERY_STRING'] and '?',
                                                    request.META['QUERY_STRING']))
        return HttpResponseRedirect(queryset[randrange(count)].get_absolute_url())


user_logger = logging.getLogger('judge.user')


class ProblemSubmit(LoginRequiredMixin, ProblemMixin, TitleMixin, SingleObjectFormView):
    template_name = 'problem/submit.html'
    form_class = ProblemSubmitForm

    @cached_property
    def contest_problem(self):
        if self.request.profile.current_contest is None:
            return None
        return get_contest_problem(self.object, self.request.profile)

    @cached_property
    def remaining_submission_count(self):
        max_subs = self.contest_problem and self.contest_problem.max_submissions
        if max_subs is None:
            return None
        # When an IE submission is rejudged into a non-IE status, it will count towards the
        # submission limit. We max with 0 to ensure that `remaining_submission_count` returns
        # a non-negative integer, which is required for future checks in this view.
        return max(
            0,
            max_subs - get_contest_submission_count(
                self.object, self.request.profile, self.request.profile.current_contest.virtual,
            ),
        )

    @cached_property
    def default_language(self):
        # If the old submission exists, use its language, otherwise use the user's default language.
        if self.old_submission is not None:
            return self.old_submission.language
        return self.request.profile.language

    def get_content_title(self):
        return mark_safe(
            escape(_('Submit to %s')) % format_html(
                '<a href="{0}">{1}</a>',
                reverse('problem_detail', args=[self.object.code]),
                self.object.translated_name(self.request.LANGUAGE_CODE),
            ),
        )

    def get_title(self):
        return _('Submit to %s') % self.object.translated_name(self.request.LANGUAGE_CODE)

    def get_initial(self):
        initial = {'language': self.default_language}
        if self.old_submission is not None:
            initial['source'] = self.old_submission.source.source
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = Submission(user=self.request.profile, problem=self.object)

        if self.object.is_vjudge or getattr(self.object, 'is_clue', False) or getattr(self.object, 'is_ntucoder', False) or getattr(self.object, 'is_ltpt', False):
            kwargs['judge_choices'] = ()
        elif self.object.is_editable_by(self.request.user):
            judges = Judge.objects.filter(online=True, problems=self.object)
            if not judges.exists():
                judges = Judge.objects.filter(online=True)
            kwargs['judge_choices'] = tuple(judges.values_list('name', 'name'))
        else:
            kwargs['judge_choices'] = ()

        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)

        if self.object.is_vjudge or getattr(self.object, 'is_clue', False) or getattr(self.object, 'is_ntucoder', False):
            form.fields['language'].queryset = self.object.allowed_languages.order_by('name', 'key')
        else:
            form.fields['language'].queryset = (
                self.object.usable_languages.order_by('name', 'key')
                .prefetch_related(Prefetch('runtimeversion_set', RuntimeVersion.objects.order_by('priority')))
            )

        form_data = getattr(form, 'cleaned_data', form.initial)
        if 'language' in form_data:
            form.fields['source'].widget.mode = form_data['language'].ace
        form.fields['source'].widget.theme = self.request.profile.resolved_ace_theme

        return form

    def get_success_url(self):
        return reverse('submission_status', args=(self.new_submission.id,))

    def form_valid(self, form):
        if not self.request.user.has_perm('judge.spam_submission'):
            if (
                Submission.objects.filter(user=self.request.profile, rejudged_date__isnull=True)
                    .exclude(status__in=['D', 'IE', 'CE', 'AB']).count() >= settings.DMOJ_SUBMISSION_LIMIT
            ) or (
                Submission.objects.filter(user=self.request.profile,
                                          date__gte=timezone.now() - settings.DMOJ_SUBMISSION_RATELIMIT_TIMEFRAME)
                    .exclude(status__in=['IE', 'CE']).count() >= settings.DMOJ_SUBMISSION_RATELIMIT
            ):
                return HttpResponse(format_html('<h1>{0}</h1>', _('You submitted too many submissions.')), status=429)
        if not self.object.allowed_languages.filter(id=form.cleaned_data['language'].id).exists():
            raise PermissionDenied()
        if not self.request.user.is_superuser and self.object.banned_users.filter(id=self.request.profile.id).exists():
            return generic_message(self.request, _('Banned from submitting'),
                                   _('You have been declared persona non grata for this problem. '
                                     'You are permanently barred from submitting to this problem.'))
        # Must check for zero and not None. None means infinite submissions remaining.
        if self.remaining_submission_count == 0:
            return generic_message(self.request, _('Too many submissions'),
                                   _('You have exceeded the submission limit for this problem.'))

        with transaction.atomic():
            self.new_submission = form.save(commit=False)

            contest_problem = self.contest_problem
            if contest_problem is not None:
                # Use the contest object from current_contest.contest because we already use it
                # in profile.update_contest().
                self.new_submission.contest_object = self.request.profile.current_contest.contest
                if self.request.profile.current_contest.live:
                    self.new_submission.locked_after = self.new_submission.contest_object.locked_after
                self.new_submission.save()
                ContestSubmission(
                    submission=self.new_submission,
                    problem=contest_problem,
                    participation=self.request.profile.current_contest,
                ).save()
            else:
                self.new_submission.save()

            source = SubmissionSource(submission=self.new_submission, source=form.cleaned_data['source'])
            source.save()

        # Save a query.
        self.new_submission.source = source

        if self.object.is_vjudge:
            from judge.tasks.vjudge_judge import judge_vjudge_submission_async
            from judge.utils.vjudge_service import get_vjudge_remote_accounts
            is_cf = (self.object.vjudge_oj or '').lower() == 'codeforces'
            default_method = 1 if is_cf else 0
            try:
                method = int(self.request.POST.get('vjudge_method', default_method))
            except (ValueError, TypeError):
                method = default_method
            binding_id = self.request.POST.get('vjudge_binding_id') or None
            try:
                open_code = int(self.request.POST.get('vjudge_open', 1))
            except (ValueError, TypeError):
                open_code = 1

            cookie = self.request.profile.vjudge_cookie if hasattr(self.request, 'profile') else None
            if (is_cf or method == 1) and not binding_id and cookie:
                remote_accs = get_vjudge_remote_accounts(cookie, oj=self.object.vjudge_oj)
                for acc in remote_accs:
                    if acc.get('isReady'):
                        binding_id = acc.get('id')
                        method = 1
                        break
                if not binding_id and remote_accs:
                    binding_id = remote_accs[0].get('id')
                    method = 1
            elif method == 0:
                binding_id = None

            judge_vjudge_submission_async(
                self.new_submission,
                method=method,
                binding_id=binding_id,
                open_code=open_code
            )
        elif getattr(self.object, 'is_clue', False):
            from judge.tasks.clue_judge import judge_clue_submission_async
            judge_clue_submission_async(self.new_submission)
        elif getattr(self.object, 'is_ntucoder', False):
            from judge.tasks.ntucoder_judge import judge_ntucoder_submission_async
            judge_ntucoder_submission_async(self.new_submission)
        elif getattr(self.object, 'is_ltpt', False):
            from judge.tasks.ltpt_judge import judge_ltpt_submission_async
            judge_ltpt_submission_async(self.new_submission)
        else:
            self.new_submission.judge(force_judge=True, judge_id=form.cleaned_data['judge'])

        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['langs'] = Language.objects.all()
        context['no_judges'] = not context['form'].fields['language'].queryset
        if self.object.is_vjudge:
            context['no_judges'] = False
            context['is_vjudge'] = True
        elif getattr(self.object, 'is_clue', False):
            context['no_judges'] = False
            context['is_clue'] = True
            context['clue_code'] = self.object.clue_code
            context['clue_has_account'] = bool(hasattr(self.request, 'profile') and self.request.profile.clue_username)
        elif getattr(self.object, 'is_ntucoder', False):
            context['no_judges'] = False
            context['is_ntucoder'] = True
            context['ntucoder_id'] = self.object.ntucoder_id
            context['ntucoder_code'] = self.object.ntucoder_code
            context['ntucoder_has_account'] = bool(hasattr(self.request, 'profile') and (self.request.profile.ntucoder_username or self.request.profile.ntucoder_email))
        elif getattr(self.object, 'is_ltpt', False):
            context['no_judges'] = False
            context['is_ltpt'] = True
            context['ltpt_code'] = self.object.ltpt_code
            context['ltpt_has_account'] = bool(hasattr(self.request, 'profile') and self.request.profile.ltpt_username)
        context['submission_limit'] = self.contest_problem and self.contest_problem.max_submissions
        context['submissions_left'] = self.remaining_submission_count
        context['ACE_URL'] = settings.ACE_URL
        context['default_lang'] = self.default_language
        return context

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except Http404:
            # Is this really necessary? This entire post() method could be removed if we don't log this.
            user_logger.info(
                'Naughty user %s wants to submit to %s without permission',
                request.user.username,
                kwargs.get(self.slug_url_kwarg),
            )
            return HttpResponseForbidden(format_html('<h1>{0}</h1>', _('Do you want me to ban you?')))

    def dispatch(self, request, *args, **kwargs):
        submission_id = kwargs.get('submission')
        if submission_id is not None:
            self.old_submission = get_object_or_404(
                Submission.objects.select_related('source', 'language'),
                id=submission_id,
            )
            if not request.user.has_perm('judge.resubmit_other') and self.old_submission.user != request.profile:
                raise PermissionDenied()
        else:
            self.old_submission = None

        return super().dispatch(request, *args, **kwargs)


class ProblemClone(ProblemMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView):
    title = gettext_lazy('Clone Problem')
    template_name = 'problem/clone.html'
    form_class = ProblemCloneForm
    permission_required = 'judge.clone_problem'

    def form_valid(self, form):
        problem = self.object

        languages = problem.allowed_languages.all()
        language_limits = problem.language_limits.all()
        organizations = problem.organizations.all()
        types = problem.types.all()
        old_code = problem.code

        problem.pk = None
        problem.is_public = False
        problem.ac_rate = 0
        problem.user_count = 0
        problem.code = form.cleaned_data['code']
        with revisions.create_revision(atomic=True):
            problem.save()
            problem.authors.add(self.request.profile)
            problem.allowed_languages.set(languages)
            problem.language_limits.set(language_limits)
            problem.organizations.set(organizations)
            problem.types.set(types)
            revisions.set_user(self.request.user)
            revisions.set_comment(_('Cloned problem from %s') % old_code)

        return HttpResponseRedirect(reverse('admin:judge_problem_change', args=(problem.id,)))



class ProblemCreateForm(forms.ModelForm):
    batch_type = forms.ChoiceField(
        choices=[("Sum", "Sum"), ("Average", "Average"), ("Points", "Points")],
        initial="Sum",
        required=True,
    )
    statement_file = forms.FileField(required=False)
    mirror_from = forms.ChoiceField(required=False)
    private_users = forms.CharField(required=False)

    class Meta:
        model = Problem
        fields = [
            "is_public", "code", "name", "time_limit", "memory_limit",
            "points", "partial", "summary", "types", "group",
            "submission_source_visibility_mode", "description",
        ]
        widgets = {
            "description": MartorWidget(attrs={"data-markdownfy-url": reverse_lazy("problem_preview")}),
            "types": Select2MultipleWidget(attrs={"style": "width: 100%;"}),
            "group": Select2Widget(attrs={"style": "max-width: 380px; width: 100%;"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].initial = ''
        self.fields['time_limit'].initial = 1.0
        self.fields['memory_limit'].initial = 1048576
        self.fields['points'].initial = 10.0
        self.fields['partial'].initial = True
        self.fields['is_public'].initial = False
        self.fields['submission_source_visibility_mode'].initial = 'A'

        uncat_type = ProblemType.objects.filter(name__iexact='uncategorized').first()
        if uncat_type:
            self.fields['types'].initial = [uncat_type.id]

        uncat_group = ProblemGroup.objects.filter(name__iexact='uncategorized').first()
        if uncat_group:
            self.fields['group'].initial = uncat_group.id

        existing = [('', '---------')] + [
            (p[0], f"{p[0]} - {p[1]}") for p in Problem.objects.values_list('code', 'name').order_by('code')
        ]
        self.fields['mirror_from'].choices = existing


class ProblemCreateView(TitleMixin, View):
    title = gettext_lazy("Creating new problem")

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

        if not (request.user.has_perm('judge.add_problem') or
                request.user.has_perm('judge.edit_all_problem') or
                request.user.has_perm('judge.change_problem') or
                request.user.is_staff or is_org_admin):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        form = ProblemCreateForm()
        existing_problems = Problem.objects.values_list('code', 'name').order_by('code')
        all_types = ProblemType.objects.all().order_by('name')
        all_groups = ProblemGroup.objects.all().order_by('name')
        uncat_type = ProblemType.objects.filter(name__iexact='uncategorized').first()
        uncat_group = ProblemGroup.objects.filter(name__iexact='uncategorized').first()
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        if self.selected_org:
            form.fields['is_public'].initial = True
        return render(request, "problem/create.html", {
            "title": self.get_title(),
            "form": form,
            "existing_problems": existing_problems,
            "all_types": all_types,
            "all_groups": all_groups,
            "default_type_id": uncat_type.id if uncat_type else None,
            "default_group_id": uncat_group.id if uncat_group else None,
            "selected_org": self.selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        form = ProblemCreateForm(request.POST, request.FILES)
        existing_problems = Problem.objects.values_list('code', 'name').order_by('code')
        all_types = ProblemType.objects.all().order_by('name')
        all_groups = ProblemGroup.objects.all().order_by('name')
        uncat_type = ProblemType.objects.filter(name__iexact='uncategorized').first()
        uncat_group = ProblemGroup.objects.filter(name__iexact='uncategorized').first()
        if not form.is_valid():
            err_msg = ""
            for field, errors in form.errors.items():
                err_msg += f"{field}: {', '.join(errors)} "
            return render(request, "problem/create.html", {
                "title": self.get_title(),
                "form": form,
                "existing_problems": existing_problems,
                "all_types": all_types,
                "all_groups": all_groups,
                "default_type_id": uncat_type.id if uncat_type else None,
                "default_group_id": uncat_group.id if uncat_group else None,
                "error": err_msg or _("Dữ liệu nhập vào chưa hợp lệ."),
            })

        raw_code = form.cleaned_data.get('code', '').strip()
        clean_code = re.sub(r'[^a-zA-Z0-9_]', '', raw_code).lower()
        if not clean_code:
            return render(request, "problem/create.html", {
                "title": self.get_title(),
                "form": form,
                "existing_problems": existing_problems,
                "error": _("Mã bài tập không hợp lệ. Vui lòng chỉ sử dụng chữ cái, chữ số và dấu gạch dưới."),
            })

        if Problem.objects.filter(code=clean_code).exists():
            return render(request, "problem/create.html", {
                "title": self.get_title(),
                "form": form,
                "existing_problems": existing_problems,
                "error": _(f"Mã bài tập '{clean_code}' đã tồn tại. Vui lòng chọn mã khác."),
            })

        org_id = request.POST.get('organization_id') or request.GET.get('org')
        target_org = None
        if org_id:
            from judge.models import Organization
            try:
                target_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                target_org = None

        with revisions.create_revision(atomic=True):
            problem = form.save(commit=False)
            problem.code = clean_code
            problem.is_manually_managed = True
            problem.date = timezone.now()
            if target_org:
                problem.is_organization_private = True
                problem.is_public = True
            problem.save()
            form.save_m2m()

            if hasattr(request, 'profile'):
                problem.authors.add(request.profile)
            if target_org:
                problem.organizations.add(target_org)
            problem.allowed_languages.set(Language.objects.all())

            # Private users
            private_users = form.cleaned_data.get('private_users', '').strip()
            if private_users:
                from judge.models import Profile
                usernames = [u.strip() for u in re.split(r'[,;\s]+', private_users) if u.strip()]
                profiles = Profile.objects.filter(user__username__in=usernames)
                problem.testers.add(*profiles)

            revisions.set_user(request.user)
            revisions.set_comment(_("Created problem via quick create form"))

        # Setup data directory
        data_dir = f"/home/dmoj/site/data/{clean_code}"
        os.makedirs(data_dir, exist_ok=True)

        mirror_from = form.cleaned_data.get('mirror_from')
        if mirror_from and Problem.objects.filter(code=mirror_from).exists():
            source_dir = f"/home/dmoj/site/data/{mirror_from}"
            if os.path.exists(source_dir):
                for item in os.listdir(source_dir):
                    s = os.path.join(source_dir, item)
                    d = os.path.join(data_dir, item)
                    if not os.path.exists(d):
                        if os.path.isdir(s):
                            shutil.copytree(s, d)
                        else:
                            shutil.copy2(s, d)

        # Handle statement file
        statement_file = request.FILES.get('statement_file')
        if statement_file:
            ext = os.path.splitext(statement_file.name)[1].lower()
            target_path = os.path.join(data_dir, f"statement{ext}")
            with open(target_path, "wb") as f:
                for chunk in statement_file.chunks():
                    f.write(chunk)

        # Ensure init.yml exists
        init_yml_path = os.path.join(data_dir, "init.yml")
        if not os.path.exists(init_yml_path):
            with open(init_yml_path, "w", encoding="utf-8") as f:
                f.write(f"# Problem {clean_code}\nchecker: standard\ntest_cases: []\n")

        return HttpResponseRedirect(reverse('problem_detail', args=[clean_code]))


class ProblemEditForm(forms.ModelForm):
    batch_type = forms.ChoiceField(
        choices=[("Sum", "Sum"), ("Average", "Average"), ("Points", "Points")],
        initial="Sum",
        required=False,
    )
    statement_file = forms.FileField(required=False)
    mirror_from = forms.ChoiceField(required=False)
    private_users = forms.CharField(required=False)

    # Editorial / Solution fields
    solution_is_public = forms.BooleanField(required=False)
    solution_publish_on = forms.DateTimeField(required=False)
    solution_authors = forms.CharField(required=False)
    solution_content = forms.CharField(
        required=False,
        widget=MartorWidget(attrs={"data-markdownfy-url": reverse_lazy("solution_preview")})
    )

    class Meta:
        model = Problem
        fields = [
            "is_public", "code", "name", "time_limit", "memory_limit",
            "points", "partial", "summary", "types", "group",
            "submission_source_visibility_mode", "description",
        ]
        widgets = {
            "description": MartorWidget(attrs={"data-markdownfy-url": reverse_lazy("problem_preview")}),
            "types": Select2MultipleWidget(attrs={"style": "width: 100%;"}),
            "group": Select2Widget(attrs={"style": "max-width: 380px; width: 100%;"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        existing = [('', '---------')] + [
            (p[0], f"{p[0]} - {p[1]}") for p in Problem.objects.values_list('code', 'name').order_by('code')
        ]
        self.fields['mirror_from'].choices = existing
        if self.instance and self.instance.pk:
            self.fields['private_users'].initial = ', '.join(
                self.instance.testers.values_list('user__username', flat=True)
            )
            if hasattr(self.instance, 'solution') and self.instance.solution:
                sol = self.instance.solution
                self.fields['solution_is_public'].initial = sol.is_public
                self.fields['solution_publish_on'].initial = sol.publish_on
                self.fields['solution_authors'].initial = ', '.join(
                    sol.authors.values_list('user__username', flat=True)
                )
                self.fields['solution_content'].initial = sol.content


class ProblemEditView(TitleMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect(reverse('auth_login') + '?next=' + request.path)
        self.problem = self.get_problem(kwargs.get('problem'))
        if not self.problem.is_editable_by(request.user):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_title(self):
        return _("Editing %s") % (self.problem.name if hasattr(self, 'problem') and self.problem else "")

    def get_problem(self, problem_key):
        if str(problem_key).isdigit():
            prob = Problem.objects.filter(id=int(problem_key)).first()
            if prob:
                return prob
        prob = Problem.objects.filter(code=problem_key).first()
        if prob:
            return prob
        raise Http404()

    def get(self, request, problem):
        form = ProblemEditForm(instance=self.problem)
        all_types = ProblemType.objects.all().order_by('name')
        all_groups = ProblemGroup.objects.all().order_by('name')
        existing_problems = Problem.objects.values_list('code', 'name').order_by('code')
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        selected_org = self.problem.organizations.first() if self.problem.is_organization_private else None

        return render(request, "problem/edit.html", {
            "title": self.get_title(),
            "problem": self.problem,
            "form": form,
            "all_types": all_types,
            "all_groups": all_groups,
            "existing_problems": existing_problems,
            "user_orgs": user_orgs,
            "selected_org": selected_org,
            "has_solution": hasattr(self.problem, 'solution') and self.problem.solution is not None,
        })

    def post(self, request, problem):
        if request.POST.get('action') == 'delete' or request.POST.get('delete_problem'):
            if request.user.is_staff or request.user.is_superuser:
                self.problem.delete()
                return HttpResponseRedirect(reverse('problem_list'))
            else:
                raise PermissionDenied()

        form = ProblemEditForm(request.POST, request.FILES, instance=self.problem)
        all_types = ProblemType.objects.all().order_by('name')
        all_groups = ProblemGroup.objects.all().order_by('name')
        existing_problems = Problem.objects.values_list('code', 'name').order_by('code')
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        selected_org = self.problem.organizations.first() if self.problem.is_organization_private else None

        if not form.is_valid():
            err_msg = ""
            for field, errors in form.errors.items():
                err_msg += f"{field}: {', '.join(errors)} "
            return render(request, "problem/edit.html", {
                "title": self.get_title(),
                "problem": self.problem,
                "form": form,
                "all_types": all_types,
                "all_groups": all_groups,
                "existing_problems": existing_problems,
                "user_orgs": user_orgs,
                "selected_org": selected_org,
                "error": err_msg or _("Dữ liệu nhập vào chưa hợp lệ."),
                "has_solution": hasattr(self.problem, 'solution') and self.problem.solution is not None,
            })

        raw_code = form.cleaned_data.get('code', '').strip()
        clean_code = re.sub(r'[^a-zA-Z0-9_]', '', raw_code).lower()
        if not clean_code:
            return render(request, "problem/edit.html", {
                "title": self.get_title(),
                "problem": self.problem,
                "form": form,
                "error": _("Mã bài tập không hợp lệ. Vui lòng chỉ sử dụng chữ cái, chữ số và dấu gạch dưới."),
                "all_types": all_types,
                "all_groups": all_groups,
                "existing_problems": existing_problems,
                "user_orgs": user_orgs,
                "selected_org": selected_org,
                "has_solution": hasattr(self.problem, 'solution') and self.problem.solution is not None,
            })

        old_code = self.problem.code
        if clean_code != old_code and Problem.objects.filter(code=clean_code).exclude(pk=self.problem.pk).exists():
            return render(request, "problem/edit.html", {
                "title": self.get_title(),
                "problem": self.problem,
                "form": form,
                "error": _(f"Mã bài tập '{clean_code}' đã tồn tại. Vui lòng chọn mã khác."),
                "all_types": all_types,
                "all_groups": all_groups,
                "existing_problems": existing_problems,
                "user_orgs": user_orgs,
                "selected_org": selected_org,
                "has_solution": hasattr(self.problem, 'solution') and self.problem.solution is not None,
            })

        with revisions.create_revision(atomic=True):
            problem_obj = form.save(commit=False)
            problem_obj.code = clean_code

            # Organization handling
            org_id = request.POST.get('organization_id')
            if org_id:
                try:
                    from judge.models import Organization
                    target_org = Organization.objects.get(id=org_id)
                    problem_obj.is_organization_private = True
                    problem_obj.is_public = True
                    problem_obj.save()
                    problem_obj.organizations.set([target_org])
                except (Organization.DoesNotExist, ValueError):
                    problem_obj.save()
            else:
                if 'organization_id' in request.POST:
                    problem_obj.is_organization_private = False
                    problem_obj.save()
                    problem_obj.organizations.clear()
                else:
                    problem_obj.save()

            form.save_m2m()

            # Handle private users (testers)
            private_users_str = form.cleaned_data.get('private_users', '').strip()
            if private_users_str:
                unames = [u.strip() for u in re.split(r'[,;\s]+', private_users_str) if u.strip()]
                problem_obj.testers.set(Profile.objects.filter(user__username__in=unames))
            else:
                problem_obj.testers.clear()

            # Handle editorial (Solution)
            solution_content = form.cleaned_data.get('solution_content', '').strip()
            if solution_content:
                sol, created = Solution.objects.get_or_create(
                    problem=problem_obj,
                    defaults={'publish_on': timezone.now()}
                )
                sol.content = solution_content
                sol.is_public = form.cleaned_data.get('solution_is_public', False)
                sol_date = form.cleaned_data.get('solution_publish_on')
                if sol_date:
                    sol.publish_on = sol_date
                elif not sol.publish_on:
                    sol.publish_on = timezone.now()
                sol.save()

                sol_authors_str = form.cleaned_data.get('solution_authors', '').strip()
                if sol_authors_str:
                    a_unames = [u.strip() for u in re.split(r'[,;\s]+', sol_authors_str) if u.strip()]
                    sol.authors.set(Profile.objects.filter(user__username__in=a_unames))
                elif hasattr(request, 'profile'):
                    sol.authors.set([request.profile])
            elif hasattr(problem_obj, 'solution') and problem_obj.solution:
                if request.POST.get('delete_solution'):
                    problem_obj.solution.delete()

            revisions.set_user(request.user)
            revisions.set_comment("Edited problem via web edit form")

        # Rename data directories if code changed
        if clean_code != old_code:
            for base_dir in ['/home/dmoj/site/data', '/home/dmoj/problems']:
                old_path = os.path.join(base_dir, old_code)
                new_path = os.path.join(base_dir, clean_code)
                if os.path.exists(old_path) and not os.path.exists(new_path):
                    try:
                        os.rename(old_path, new_path)
                    except Exception:
                        pass

        # Handle statement file upload
        data_dir = f"/home/dmoj/site/data/{clean_code}"
        os.makedirs(data_dir, exist_ok=True)

        statement_file = request.FILES.get('statement_file')
        if statement_file:
            ext = os.path.splitext(statement_file.name)[1].lower()
            target_path = os.path.join(data_dir, f"statement{ext}")
            with open(target_path, "wb") as f:
                for chunk in statement_file.chunks():
                    f.write(chunk)

        # Handle mirror test data
        mirror_from = form.cleaned_data.get('mirror_from')
        if mirror_from and Problem.objects.filter(code=mirror_from).exists():
            source_dir = f"/home/dmoj/site/data/{mirror_from}"
            if os.path.exists(source_dir):
                for item in os.listdir(source_dir):
                    s = os.path.join(source_dir, item)
                    d = os.path.join(data_dir, item)
                    if not os.path.exists(d):
                        if os.path.isdir(s):
                            shutil.copytree(s, d)
                        else:
                            shutil.copy2(s, d)

        return HttpResponseRedirect(reverse('problem_detail', args=[clean_code]))


class ImportPolygonView(TitleMixin, View):
    title = gettext_lazy("Import Problem from Polygon")

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        org_id = request.GET.get('org')
        selected_org = None
        if org_id:
            from judge.models import Organization
            try:
                selected_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                selected_org = None
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        return render(request, "problem/import_polygon.html", {
            "title": self.get_title(),
            "selected_org": selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        zip_file = request.FILES.get("zip_file")
        if not zip_file:
            return render(request, "problem/import_polygon.html", {
                "title": self.get_title(),
                "error": _("Please select a Polygon package (.zip file) to upload."),
            })

        if not zip_file.name.lower().endswith(".zip"):
            return render(request, "problem/import_polygon.html", {
                "title": self.get_title(),
                "error": _("The uploaded file must be a .zip file."),
            })

        code_override = request.POST.get("code", "").strip() or None
        name_override = request.POST.get("name", "").strip() or None
        points_override = request.POST.get("points", "").strip() or None
        time_limit_override = request.POST.get("time_limit", "").strip() or None
        memory_limit_override = request.POST.get("memory_limit", "").strip() or None
        is_public = bool(request.POST.get("is_public"))

        from judge.utils.polygon_importer import import_polygon_package

        try:
            problem, test_count = import_polygon_package(
                zip_file=zip_file,
                code_override=code_override,
                name_override=name_override,
                points_override=points_override,
                time_limit_override=time_limit_override,
                memory_limit_override=memory_limit_override,
                is_public=is_public,
                author_profile=(request.profile if (request.user.is_authenticated and hasattr(request, "profile")) else None) or Profile.objects.filter(user__is_superuser=True).first(),
            )
            org_id = request.POST.get('organization_id') or request.GET.get('org')
            if org_id:
                from judge.models import Organization
                try:
                    target_org = Organization.objects.get(id=org_id)
                    problem.is_organization_private = True
                    problem.is_public = True
                    problem.organizations.add(target_org)
                    problem.save()
                except (Organization.DoesNotExist, ValueError):
                    pass
            return HttpResponseRedirect(reverse("problem_detail", args=[problem.code]))
        except Exception as e:
            return render(request, "problem/import_polygon.html", {
                "title": self.get_title(),
                "error": str(e),
                "code": code_override or "",
                "name": name_override or "",
                "points": points_override or "",
                "time_limit": time_limit_override or "",
                "memory_limit": memory_limit_override or "",
            })

class ImportVJudgeView(TitleMixin, View):
    title = gettext_lazy("Import Problem from VJudge")

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        org_id = request.GET.get('org')
        selected_org = None
        if org_id:
            from judge.models import Organization
            try:
                selected_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                selected_org = None
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        return render(request, "problem/import_vjudge.html", {
            "title": self.get_title(),
            "selected_org": selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        vjudge_input = request.POST.get("vjudge_input", "").strip()
        if not vjudge_input:
            return render(request, "problem/import_vjudge.html", {
                "title": self.get_title(),
                "error": _("Please enter a VJudge problem ID or URL."),
            })

        code_override = request.POST.get("code", "").strip() or None
        name_override = request.POST.get("name", "").strip() or None
        points_override = request.POST.get("points", "").strip() or None
        time_limit_override = request.POST.get("time_limit", "").strip() or None
        memory_limit_override = request.POST.get("memory_limit", "").strip() or None
        is_public = bool(request.POST.get("is_public"))

        from judge.utils.vjudge_importer import import_vjudge_problem

        try:
            problem, created = import_vjudge_problem(
                vjudge_input=vjudge_input,
                code_override=code_override,
                name_override=name_override,
                points_override=points_override,
                time_limit_override=time_limit_override,
                memory_limit_override=memory_limit_override,
                is_public=is_public,
                author_profile=(request.profile if (request.user.is_authenticated and hasattr(request, "profile")) else None),
            )
            org_id = request.POST.get('organization_id') or request.GET.get('org')
            if org_id:
                from judge.models import Organization
                try:
                    target_org = Organization.objects.get(id=org_id)
                    problem.is_organization_private = True
                    problem.is_public = True
                    problem.organizations.add(target_org)
                    problem.save()
                except (Organization.DoesNotExist, ValueError):
                    pass
            return HttpResponseRedirect(reverse("problem_detail", args=[problem.code]))
        except Exception as e:
            return render(request, "problem/import_vjudge.html", {
                "title": self.get_title(),
                "error": str(e),
                "vjudge_input": vjudge_input,
                "code": code_override or "",
                "name": name_override or "",
                "points": points_override or "",
                "time_limit": time_limit_override or "",
                "memory_limit": memory_limit_override or "",
            })


class ImportClueView(TitleMixin, View):
    title = gettext_lazy("Import Problem from ClueOJ")

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        org_id = request.GET.get('org')
        selected_org = None
        if org_id:
            from judge.models import Organization
            try:
                selected_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                selected_org = None
        user_orgs = request.profile.organizations.all() if hasattr(request, 'profile') else []
        return render(request, "problem/import_clue.html", {
            "title": self.get_title(),
            "active_tab": request.GET.get("tab", "public"),
            "selected_org": selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        clue_input = request.POST.get("clue_input", "").strip()
        if not clue_input:
            return render(request, "problem/import_clue.html", {
                "title": self.get_title(),
                "error": _("Vui lòng nhập mã bài hoặc URL bài tập từ ClueOJ."),
                "active_tab": request.POST.get("import_type", "public"),
            })

        import_type = request.POST.get("import_type", "public")
        code_override = request.POST.get("code", "").strip() or None
        name_override = request.POST.get("name", "").strip() or None
        points_override = request.POST.get("points", "").strip() or None
        time_limit_override = request.POST.get("time_limit", "").strip() or None
        memory_limit_override = request.POST.get("memory_limit", "").strip() or None
        is_public = bool(request.POST.get("is_public"))
        author_profile = (request.profile if (request.user.is_authenticated and hasattr(request, "profile")) else None) or Profile.objects.filter(user__is_superuser=True).first()

        try:
            if import_type == "org" or request.POST.get("is_org_import"):
                from judge.utils.clue_importer import import_clue_organization_problem
                clue_username = request.POST.get("clue_username", "").strip()
                clue_password = request.POST.get("clue_password", "").strip()
                organization = request.POST.get("organization", "csattutor").strip()
                problem, test_count = import_clue_organization_problem(
                    clue_input=clue_input,
                    clue_username=clue_username,
                    clue_password=clue_password,
                    organization=organization,
                    code_override=code_override,
                    name_override=name_override,
                    points_override=points_override,
                    time_limit_override=time_limit_override,
                    memory_limit_override=memory_limit_override,
                    is_public=is_public,
                    author_profile=author_profile,
                )
            else:
                from judge.utils.clue_importer import import_clue_problem
                problem = import_clue_problem(
                    clue_input=clue_input,
                    code_override=code_override,
                    name_override=name_override,
                    points_override=points_override,
                    time_limit_override=time_limit_override,
                    memory_limit_override=memory_limit_override,
                    is_public=is_public,
                    author_profile=author_profile,
                )
                if isinstance(problem, (tuple, list)):
                    problem = problem[0]
            org_id = request.POST.get('organization_id') or request.GET.get('org')
            if org_id:
                from judge.models import Organization
                try:
                    target_org = Organization.objects.get(id=org_id)
                    problem.is_organization_private = True
                    problem.is_public = True
                    problem.organizations.add(target_org)
                    problem.save()
                except (Organization.DoesNotExist, ValueError):
                    pass
            return HttpResponseRedirect(reverse("problem_detail", args=[problem.code]))
        except Exception as e:
            return render(request, "problem/import_clue.html", {
                "title": self.get_title(),
                "error": str(e),
                "clue_input": clue_input,
                "active_tab": import_type,
                "code": code_override or "",
                "name": name_override or "",
                "points": points_override or "",
                "time_limit": time_limit_override or "",
                "memory_limit": memory_limit_override or "",
                "clue_username": request.POST.get("clue_username", ""),
                "organization": request.POST.get("organization", "csattutor"),
            })
class VJudgeStatementAjaxView(View):
    def get(self, request, problem):
        from judge.models import Problem
        from judge.utils.vjudge_service import (
            get_vjudge_problem_data,
            get_vjudge_statement_content,
            select_best_vjudge_statement,
        )
        prob = get_object_or_404(Problem, code=problem)
        key = request.GET.get('key')
        lang = request.GET.get('lang')
        cookie = request.profile.vjudge_cookie if request.user.is_authenticated and hasattr(request, 'profile') else None

        if lang and not key:
            data = get_vjudge_problem_data(prob.vjudge_oj, prob.vjudge_prob_num, cookie=cookie)
            statements = data.get('descBriefs', []) if data else []
            best_stmt = select_best_vjudge_statement(statements, user_lang=lang)
            if best_stmt and best_stmt.get('key'):
                key = str(best_stmt['key'])
                author = best_stmt.get('author') or 'VJudge'
                lang_display = best_stmt.get('langDisplay') or best_stmt.get('lang') or lang
                html = get_vjudge_statement_content(key, cookie=cookie)
                return JsonResponse({'success': True, 'html': html, 'key': key, 'author': author, 'lang': lang_display})
            elif lang.startswith('vi'):
                vi_trans = prob.translations.filter(language__startswith='vi').first()
                desc_text = vi_trans.description if vi_trans else prob.description
                return JsonResponse({'success': True, 'html': desc_text, 'lang': 'Tiếng Việt', 'author': 'System'})
            elif lang.startswith('en'):
                en_trans = prob.translations.filter(language__startswith='en').first()
                html = en_trans.description if en_trans else prob.description
                return JsonResponse({'success': True, 'html': html, 'lang': 'English', 'author': 'System'})

        if not key:
            return JsonResponse({'success': False, 'error': 'Missing key or lang'}, status=400)

        html = get_vjudge_statement_content(key, cookie=cookie)
        return JsonResponse({'success': True, 'html': html, 'key': key})
