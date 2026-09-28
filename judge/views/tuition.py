import json
from datetime import datetime
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db.models import Q
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from judge.models import Contest, ContestParticipation, Organization, Profile
from judge.models.tuition import (
    ContestTuition,
    OrganizationTuition,
    StudentSessionExclusion,
    StudentTuitionPayment,
)


def fmt_vnd(amount):
    return f"{amount:,}".replace(',', '.') + " đ"


class AdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        if hasattr(user, 'profile') and user.profile and user.profile.admin_of.exists():
            return True
        return False

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return HttpResponseRedirect(reverse('auth_login') + '?next=' + self.request.path)
        return HttpResponseForbidden(_("Bạn không có quyền truy cập trang quản lý học phí."))


class TuitionDashboardView(AdminRequiredMixin, View):
    def get(self, request):
        user = request.user
        profile = getattr(user, 'profile', None)

        # 1. Accessible Organizations
        if user.is_superuser or user.is_staff:
            organizations = Organization.objects.all().order_by('name')
        else:
            organizations = profile.admin_of.all().order_by('name') if profile else Organization.objects.none()

        if not organizations.exists():
            return render(request, "tuition/dashboard.html", {
                "title": _("Quản lý tiền dạy học"),
                "organizations": [],
                "selected_org": None,
                "error": _("Chưa có tổ chức hoặc lớp học nào được tạo trên hệ thống."),
            })

        # 2. Selected Organization
        selected_org_id = request.GET.get('org')
        selected_org = None
        if selected_org_id:
            try:
                selected_org = organizations.get(id=int(selected_org_id))
            except (Organization.DoesNotExist, ValueError):
                selected_org = organizations.first()
        else:
            selected_org = organizations.first()

        # 3. Organization Tuition Settings
        org_tuition, created = OrganizationTuition.objects.get_or_create(
            organization=selected_org,
            defaults={
                'billing_type': 'individual',
                'default_fee_per_session': 200000,
            }
        )

        # 4. Period / Month Filter
        selected_period = request.GET.get('period', 'all').strip()

        # 5. Contests / Sessions of this Organization
        contest_qs = Contest.objects.filter(organizations=selected_org).order_by('-start_time')
        if selected_period and selected_period != 'all':
            try:
                year, month = map(int, selected_period.split('-'))
                contest_qs = contest_qs.filter(start_time__year=year, start_time__month=month)
            except Exception:
                pass

        contests = list(contest_qs)

        # Preload ContestTuition overrides
        contest_tuition_map = {
            ct.contest_id: ct
            for ct in ContestTuition.objects.filter(contest__in=contests)
        }

        # Build contest list with effective fees and session counts
        contest_data_list = []
        for c in contests:
            ct = contest_tuition_map.get(c.id)
            session_count = ct.session_count if (ct and ct.session_count is not None) else 1
            if session_count < 1:
                session_count = 1

            if ct and ct.fee_override is not None:
                effective_fee = ct.fee_override
            else:
                effective_fee = session_count * org_tuition.default_fee_per_session

            is_billed = ct.is_billed if ct else True
            participant_count = c.users.count()

            contest_data_list.append({
                'id': c.id,
                'key': c.key,
                'name': c.name,
                'start_time': c.start_time,
                'start_time_fmt': c.start_time.strftime('%d/%m/%Y %H:%M') if c.start_time else '',
                'session_count': session_count,
                'fee_override': ct.fee_override if ct else None,
                'effective_fee': effective_fee,
                'effective_fee_fmt': fmt_vnd(effective_fee),
                'is_billed': is_billed,
                'participant_count': participant_count,
                'note': ct.note if ct else '',
            })

        # 6. Teachers / Admins Identification
        org_admins = list(selected_org.admins.all().select_related('user'))
        org_admin_ids = set(p.id for p in org_admins)

        # Check toggle to show admins (default: False, teachers are NOT in student payment list)
        include_admins = request.GET.get('include_admins', '0') == '1'

        # Students belonging to this organization (or who joined its contests)
        student_profiles_qs = Profile.objects.filter(
            Q(organizations=selected_org) | Q(contest_history__contest__in=contests)
        ).distinct().select_related('user').order_by('user__username')

        if not include_admins:
            # Exclude organization admins and superusers from the student list
            student_profiles_qs = student_profiles_qs.exclude(id__in=org_admin_ids).exclude(user__is_superuser=True)

        student_profiles = list(student_profiles_qs)

        # Map: contest_id -> set of participated profile_ids
        participation_map = {}
        for c in contests:
            p_ids = set(ContestParticipation.objects.filter(contest=c).values_list('user_id', flat=True))
            participation_map[c.id] = p_ids

        # Preload StudentSessionExclusion: set of (profile_id, contest_id)
        exclusion_set = set(
            StudentSessionExclusion.objects.filter(organization=selected_org)
            .values_list('profile_id', 'contest_id')
        )

        # Preload StudentTuitionPayment records
        payment_records = {
            p.profile_id: p
            for p in StudentTuitionPayment.objects.filter(
                organization=selected_org,
                period=selected_period
            )
        }

        # 7. Compute Tuition Statistics per Student
        students_stats = []
        total_expected_revenue = 0
        total_collected_amount = 0
        total_attendance_count = 0
        total_absence_count = 0

        total_sessions_count = sum(cdata['session_count'] for cdata in contest_data_list if cdata['is_billed'])

        for sp in student_profiles:
            sp_id = sp.id
            attended_sessions = 0
            absent_sessions = 0
            billable_sessions = 0
            excluded_sessions = 0
            calculated_fee = 0

            for cdata in contest_data_list:
                c_id = cdata['id']
                has_attended = sp_id in participation_map.get(c_id, set())
                is_excluded = (sp_id, c_id) in exclusion_set
                cnt = cdata['session_count']
                session_fee = cdata['effective_fee']

                if has_attended:
                    attended_sessions += cnt
                else:
                    absent_sessions += cnt

                if not cdata['is_billed'] or is_excluded:
                    if is_excluded:
                        excluded_sessions += cnt
                    continue

                if has_attended:
                    billable_sessions += cnt
                    calculated_fee += session_fee
                else:
                    if org_tuition.billing_type == 'center':
                        # Trung tâm: vắng vẫn tính tiền
                        billable_sessions += cnt
                        calculated_fee += session_fee

            payment_obj = payment_records.get(sp_id)
            paid_amt = payment_obj.paid_amount if payment_obj else 0
            status = payment_obj.status if payment_obj else ('paid' if calculated_fee == 0 else 'unpaid')
            payment_note = payment_obj.note if payment_obj else ''

            remaining_amt = max(0, calculated_fee - paid_amt)

            total_expected_revenue += calculated_fee
            total_collected_amount += paid_amt
            total_attendance_count += attended_sessions
            total_absence_count += absent_sessions

            students_stats.append({
                'profile': sp,
                'profile_id': sp.id,
                'username': sp.user.username,
                'display_name': sp.display_name or sp.user.username,
                'attended_sessions': attended_sessions,
                'absent_sessions': absent_sessions,
                'billable_sessions': billable_sessions,
                'excluded_sessions': excluded_sessions,
                'rate_per_session': org_tuition.default_fee_per_session,
                'rate_per_session_fmt': fmt_vnd(org_tuition.default_fee_per_session),
                'total_fee': calculated_fee,
                'total_fee_fmt': fmt_vnd(calculated_fee),
                'paid_amount': paid_amt,
                'paid_amount_fmt': fmt_vnd(paid_amt),
                'remaining_amount': remaining_amt,
                'remaining_amount_fmt': fmt_vnd(remaining_amt),
                'status': status,
                'note': payment_note,
            })

        total_outstanding_amount = max(0, total_expected_revenue - total_collected_amount)

        # Available periods for filter dropdown (e.g. recent months)
        all_dates = Contest.objects.filter(organizations=selected_org).dates('start_time', 'month', order='DESC')
        available_periods = [d.strftime('%Y-%m') for d in all_dates]

        return render(request, "tuition/dashboard.html", {
            "title": _("Quản lý tiền dạy học"),
            "organizations": organizations,
            "selected_org": selected_org,
            "org_tuition": org_tuition,
            "org_admins": org_admins,
            "include_admins": include_admins,
            "selected_period": selected_period,
            "available_periods": available_periods,
            "contests": contest_data_list,
            "students_stats": students_stats,
            "total_contests": len(contest_data_list),
            "total_sessions": total_sessions_count,
            "total_students": len(students_stats),
            "total_attendance_count": total_attendance_count,
            "total_absence_count": total_absence_count,
            "total_expected_revenue": total_expected_revenue,
            "total_expected_revenue_fmt": fmt_vnd(total_expected_revenue),
            "total_collected_amount": total_collected_amount,
            "total_collected_amount_fmt": fmt_vnd(total_collected_amount),
            "total_outstanding_amount": total_outstanding_amount,
            "total_outstanding_amount_fmt": fmt_vnd(total_outstanding_amount),
        })


class TuitionSettingsView(AdminRequiredMixin, View):
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            org = get_object_or_404(Organization, id=org_id)

            if not (request.user.is_superuser or request.user.is_staff):
                if not (request.profile and request.profile.admin_of.filter(id=org_id).exists()):
                    return JsonResponse({'status': 'error', 'message': _('Không có quyền thay đổi tổ chức này.')}, status=403)

            billing_type = request.POST.get('billing_type', 'individual').strip()
            if billing_type not in ('individual', 'center'):
                billing_type = 'individual'

            fee_raw = request.POST.get('default_fee_per_session', '200000').replace(',', '').replace('.', '').strip()
            default_fee = int(fee_raw) if fee_raw.isdigit() else 200000

            note = request.POST.get('note', '').strip()

            setting, created = OrganizationTuition.objects.update_or_create(
                organization=org,
                defaults={
                    'billing_type': billing_type,
                    'default_fee_per_session': default_fee,
                    'note': note,
                }
            )

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã cập nhật cài đặt học phí thành công!'),
                'billing_type': setting.billing_type,
                'billing_type_display': setting.get_billing_type_display(),
                'default_fee': setting.default_fee_per_session,
                'default_fee_fmt': fmt_vnd(setting.default_fee_per_session),
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionPaymentUpdateView(AdminRequiredMixin, View):
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            profile_id = int(request.POST.get('profile_id'))
            period = request.POST.get('period', 'all').strip()

            paid_raw = request.POST.get('paid_amount', '0').replace(',', '').replace('.', '').strip()
            paid_amount = int(paid_raw) if paid_raw.isdigit() else 0

            status = request.POST.get('status', 'unpaid').strip()
            if status not in ('unpaid', 'partial', 'paid'):
                status = 'unpaid'

            note = request.POST.get('note', '').strip()

            record, created = StudentTuitionPayment.objects.update_or_create(
                organization_id=org_id,
                profile_id=profile_id,
                period=period,
                defaults={
                    'paid_amount': paid_amount,
                    'status': status,
                    'note': note,
                }
            )

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã lưu trạng thái thanh toán!'),
                'paid_amount': record.paid_amount,
                'paid_amount_fmt': fmt_vnd(record.paid_amount),
                'status_val': record.status,
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionDeletePaymentView(AdminRequiredMixin, View):
    """Xóa số tiền đã nộp / reset thanh toán về 0 đ"""
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            profile_id = int(request.POST.get('profile_id'))
            period = request.POST.get('period', 'all').strip()

            StudentTuitionPayment.objects.filter(
                organization_id=org_id,
                profile_id=profile_id,
                period=period,
            ).delete()

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã xóa thông tin thanh toán (reset về 0 đ)!'),
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionContestFeeView(AdminRequiredMixin, View):
    """Chỉnh sửa chi phí và số buổi học của từng contest"""
    def post(self, request):
        try:
            contest_id = int(request.POST.get('contest_id'))
            contest = get_object_or_404(Contest, id=contest_id)

            session_count_raw = request.POST.get('session_count', '1').strip()
            session_count = int(session_count_raw) if session_count_raw.isdigit() else 1
            if session_count < 1:
                session_count = 1

            fee_raw = request.POST.get('fee_override', '').replace(',', '').replace('.', '').strip()
            fee_override = int(fee_raw) if fee_raw.isdigit() else None

            is_billed = request.POST.get('is_billed', 'true').lower() in ('true', '1', 'yes')
            note = request.POST.get('note', '').strip()

            ct, created = ContestTuition.objects.update_or_create(
                contest=contest,
                defaults={
                    'session_count': session_count,
                    'fee_override': fee_override,
                    'is_billed': is_billed,
                    'note': note,
                }
            )

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã cập nhật chi phí cho buổi học %(name)s!') % {'name': contest.name},
                'session_count': ct.session_count,
                'fee_override': ct.fee_override,
                'fee_override_fmt': fmt_vnd(ct.fee_override) if ct.fee_override else None,
                'is_billed': ct.is_billed,
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionUnlinkContestView(AdminRequiredMixin, View):
    """Xóa một buổi học (contest) khỏi lớp học/tổ chức này"""
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            contest_id = int(request.POST.get('contest_id'))

            org = get_object_or_404(Organization, id=org_id)
            contest = get_object_or_404(Contest, id=contest_id)

            contest.organizations.remove(org)

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã xóa buổi học %(name)s khỏi tổ chức!') % {'name': contest.name},
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionStudentDetailView(AdminRequiredMixin, View):
    """Xem chi tiết từng buổi học của học viên và hỗ trợ xóa buổi trực tiếp"""
    def get(self, request):
        try:
            org_id = int(request.GET.get('org_id'))
            profile_id = int(request.GET.get('profile_id'))
            period = request.GET.get('period', 'all').strip()

            org = get_object_or_404(Organization, id=org_id)
            student_profile = get_object_or_404(Profile, id=profile_id)

            org_tuition, created = OrganizationTuition.objects.get_or_create(
                organization=org,
                defaults={'billing_type': 'individual', 'default_fee_per_session': 200000}
            )

            contest_qs = Contest.objects.filter(organizations=org).order_by('-start_time')
            if period and period != 'all':
                try:
                    year, month = map(int, period.split('-'))
                    contest_qs = contest_qs.filter(start_time__year=year, start_time__month=month)
                except Exception:
                    pass

            contests = list(contest_qs)
            contest_tuition_map = {
                ct.contest_id: ct
                for ct in ContestTuition.objects.filter(contest__in=contests)
            }

            # Preload exclusions for this student in this org
            excluded_contest_ids = set(
                StudentSessionExclusion.objects.filter(
                    organization=org,
                    profile=student_profile
                ).values_list('contest_id', flat=True)
            )

            sessions = []
            total_billable = 0
            total_amount = 0

            for c in contests:
                ct = contest_tuition_map.get(c.id)
                session_count = ct.session_count if (ct and ct.session_count is not None) else 1
                if session_count < 1:
                    session_count = 1

                if ct and ct.fee_override is not None:
                    fee = ct.fee_override
                else:
                    fee = session_count * org_tuition.default_fee_per_session

                is_billed = ct.is_billed if ct else True

                part = ContestParticipation.objects.filter(contest=c, user=student_profile).first()
                attended = bool(part)
                score = part.score if part else None
                subs_count = part.submissions.count() if part else 0

                is_excluded = c.id in excluded_contest_ids

                # Charged status
                charged = False
                charge_amount = 0
                charge_reason = ""

                if is_excluded:
                    charged = False
                    charge_amount = 0
                    charge_reason = _("Đã xóa khỏi học phí (Miễn tính tiền buổi này)")
                elif is_billed:
                    if attended:
                        charged = True
                        charge_amount = fee
                        charge_reason = _("Có mặt đi học") + (f" ({session_count} buổi)" if session_count > 1 else "")
                        total_billable += session_count
                        total_amount += fee
                    elif org_tuition.billing_type == 'center':
                        charged = True
                        charge_amount = fee
                        charge_reason = _("Vắng mặt (tính phí theo trung tâm)") + (f" ({session_count} buổi)" if session_count > 1 else "")
                        total_billable += session_count
                        total_amount += fee
                    else:
                        charged = False
                        charge_amount = 0
                        charge_reason = _("Vắng mặt (dạy kèm riêng: miễn phí)")
                else:
                    charge_reason = _("Buổi học miễn phí / không tính tiền")

                sessions.append({
                    'contest_id': c.id,
                    'contest_key': c.key,
                    'contest_name': c.name,
                    'start_time': c.start_time.strftime('%d/%m/%Y %H:%M') if c.start_time else '',
                    'session_count': session_count,
                    'attended': attended,
                    'has_participation': bool(part),
                    'score': score,
                    'submissions': subs_count,
                    'fee': fee,
                    'fee_fmt': fmt_vnd(fee),
                    'fee_override': ct.fee_override if ct else None,
                    'is_billed': is_billed,
                    'is_excluded': is_excluded,
                    'charged': charged,
                    'charge_amount': charge_amount,
                    'charge_amount_fmt': fmt_vnd(charge_amount),
                    'charge_reason': charge_reason,
                })

            payment_obj = StudentTuitionPayment.objects.filter(
                organization=org,
                profile=student_profile,
                period=period
            ).first()

            paid_amount = payment_obj.paid_amount if payment_obj else 0
            status = payment_obj.status if payment_obj else ('paid' if total_amount == 0 else 'unpaid')
            payment_note = payment_obj.note if payment_obj else ''

            return JsonResponse({
                'status': 'ok',
                'student': {
                    'profile_id': student_profile.id,
                    'username': student_profile.user.username,
                    'display_name': student_profile.display_name or student_profile.user.username,
                },
                'organization': {
                    'id': org.id,
                    'name': org.name,
                },
                'billing_type': org_tuition.billing_type,
                'billing_type_display': org_tuition.get_billing_type_display(),
                'default_fee': org_tuition.default_fee_per_session,
                'default_fee_fmt': fmt_vnd(org_tuition.default_fee_per_session),
                'total_billable_sessions': total_billable,
                'total_amount': total_amount,
                'total_amount_fmt': fmt_vnd(total_amount),
                'paid_amount': paid_amount,
                'paid_amount_fmt': fmt_vnd(paid_amount),
                'remaining_amount': max(0, total_amount - paid_amount),
                'remaining_amount_fmt': fmt_vnd(max(0, total_amount - paid_amount)),
                'payment_status': status,
                'payment_note': payment_note,
                'sessions': sessions,
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionExcludeSessionView(AdminRequiredMixin, View):
    """Xóa / loại trừ một buổi học cụ thể của học viên khỏi tính tiền"""
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            profile_id = int(request.POST.get('profile_id'))
            contest_id = int(request.POST.get('contest_id'))
            remove_part = request.POST.get('remove_participation', 'false').lower() in ('true', '1', 'yes')

            org = get_object_or_404(Organization, id=org_id)
            student_profile = get_object_or_404(Profile, id=profile_id)
            contest = get_object_or_404(Contest, id=contest_id)

            # Create exclusion record
            StudentSessionExclusion.objects.get_or_create(
                organization=org,
                profile=student_profile,
                contest=contest,
                defaults={'note': _('Xóa buổi học bởi quản trị viên')}
            )

            # Optionally delete contest participation
            if remove_part:
                ContestParticipation.objects.filter(contest=contest, user=student_profile).delete()

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã xóa buổi học %(contest)s khỏi học phí của %(student)s!') % {
                    'contest': contest.name,
                    'student': student_profile.user.username,
                },
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


class TuitionRestoreSessionView(AdminRequiredMixin, View):
    """Khôi phục lại buổi học đã bị xóa cho học viên"""
    def post(self, request):
        try:
            org_id = int(request.POST.get('org_id'))
            profile_id = int(request.POST.get('profile_id'))
            contest_id = int(request.POST.get('contest_id'))

            StudentSessionExclusion.objects.filter(
                organization_id=org_id,
                profile_id=profile_id,
                contest_id=contest_id,
            ).delete()

            return JsonResponse({
                'status': 'ok',
                'message': _('Đã khôi phục tính tiền cho buổi học!'),
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
