import logging
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _, gettext
from django.views import View

from judge.utils.ltpt_importer import import_ltpt_problem
from judge.utils.ltpt_service import check_ltpt_login, login_ltpt
from judge.utils.views import TitleMixin

logger = logging.getLogger('judge.ltpt_views')

class LTPTConnectView(LoginRequiredMixin, TitleMixin, View):
    title = _("Kết nối tài khoản Lập trình phổ thông (LTPT)")

    def get(self, request):
        profile = request.profile
        cookie = profile.ltpt_cookie
        is_connected = False

        if cookie:
            check = check_ltpt_login(cookie)
            is_connected = check.get("logged_in", False)
        elif profile.ltpt_username and profile.ltpt_password:
            res = login_ltpt(profile.ltpt_username, profile.ltpt_password)
            if res.get("success"):
                profile.ltpt_cookie = res["cookie"]
                profile.save(update_fields=["ltpt_cookie"])
                is_connected = True

        return render(request, "ltpt/connect.html", {
            "title": self.get_title(),
            "profile": profile,
            "is_connected": is_connected,
            "next_url": request.GET.get("next") or "",
            "ltpt_username": profile.ltpt_username or "",
        })

    def post(self, request):
        profile = request.profile
        ltpt_username = request.POST.get("ltpt_username", "").strip()
        ltpt_password = request.POST.get("ltpt_password", "").strip()
        next_url = request.GET.get("next") or request.POST.get("next") or ""

        if not ltpt_username or not ltpt_password:
            return render(request, "ltpt/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": _("Vui lòng nhập đầy đủ Tên đăng nhập và Mật khẩu LapTrinhPhoThong."),
                "ltpt_username": ltpt_username,
                "next_url": next_url,
            })

        res = login_ltpt(ltpt_username, ltpt_password)
        if not res.get("success"):
            return render(request, "ltpt/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": res.get("error", _("Đăng nhập LTPT thất bại. Vui lòng kiểm tra lại tài khoản và mật khẩu.")),
                "ltpt_username": ltpt_username,
                "next_url": next_url,
            })

        profile.ltpt_username = ltpt_username
        profile.ltpt_password = ltpt_password
        profile.ltpt_cookie = res["cookie"]
        profile.save(update_fields=["ltpt_username", "ltpt_password", "ltpt_cookie"])

        if not next_url:
            next_url = reverse("ltpt_connect")

        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax"):
            return JsonResponse({
                "success": True,
                "message": gettext("Kết nối tài khoản LapTrinhPhoThong thành công!"),
                "username": ltpt_username,
            })

        return HttpResponseRedirect(next_url)

class LTPTDisconnectView(LoginRequiredMixin, View):
    def post(self, request):
        profile = request.profile
        profile.ltpt_cookie = ""
        profile.ltpt_password = ""
        profile.ltpt_username = ""
        profile.save(update_fields=["ltpt_cookie", "ltpt_password", "ltpt_username"])

        next_url = request.GET.get("next") or request.POST.get("next") or reverse("ltpt_connect")
        return HttpResponseRedirect(next_url)

class ProblemImportLTPTView(LoginRequiredMixin, TitleMixin, View):
    title = _("Nhập bài tập từ Lập trình phổ thông (LTPT)")

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
        is_org_admin = (selected_org and hasattr(request, 'profile') and (
            selected_org.admins.filter(id=request.profile.id).exists() or
            selected_org.members.filter(id=request.profile.id).exists()
        ))
        if not (request.user.is_staff or request.user.has_perm('judge.edit_all_problem') or request.user.has_perm('judge.edit_own_problem') or is_org_admin):
            return render(request, "403.html", status=403)

        return render(request, "problem/import_ltpt.html", {
            "title": self.get_title(),
            "profile": request.profile,
            "selected_org": selected_org,
            "user_orgs": user_orgs,
        })

    def post(self, request):
        org_id = request.POST.get('organization_id') or request.GET.get('org')
        target_org = None
        if org_id:
            from judge.models import Organization
            try:
                target_org = Organization.objects.get(id=org_id)
            except (Organization.DoesNotExist, ValueError):
                target_org = None
        is_org_admin = (target_org and hasattr(request, 'profile') and (
            target_org.admins.filter(id=request.profile.id).exists() or
            target_org.members.filter(id=request.profile.id).exists()
        ))
        if not (request.user.is_staff or request.user.has_perm('judge.edit_all_problem') or request.user.has_perm('judge.edit_own_problem') or is_org_admin):
            return render(request, "403.html", status=403)

        ltpt_input = request.POST.get("ltpt_input", "").strip()
        code_override = request.POST.get("code_override", "").strip() or None
        name_override = request.POST.get("name_override", "").strip() or None
        points_override = request.POST.get("points_override", "").strip() or None
        time_limit_override = request.POST.get("time_limit_override", "").strip() or None
        memory_limit_override = request.POST.get("memory_limit_override", "").strip() or None
        is_public = request.POST.get("is_public") == "on"

        if not ltpt_input:
            return render(request, "problem/import_ltpt.html", {
                "title": self.get_title(),
                "error": _("Vui lòng nhập mã bài hoặc liên kết bài tập từ LapTrinhPhoThong (ví dụ: a01a000005 hoặc https://laptrinhphothong.vn/problem/a01a000005)."),
                "ltpt_input": ltpt_input,
                "code_override": code_override or "",
                "name_override": name_override or "",
            })

        try:
            problem = import_ltpt_problem(
                ltpt_input=ltpt_input,
                code_override=code_override,
                name_override=name_override,
                points_override=points_override,
                time_limit_override=time_limit_override,
                memory_limit_override=memory_limit_override,
                is_public=is_public,
                author_profile=request.profile,
            )
            if target_org:
                problem.is_organization_private = True
                problem.is_public = True
                problem.organizations.add(target_org)
                problem.save()
            return HttpResponseRedirect(reverse("problem_detail", args=[problem.code]))
        except Exception as e:
            logger.exception(f"ProblemImportLTPT error: {e}")
            return render(request, "problem/import_ltpt.html", {
                "title": self.get_title(),
                "error": str(e),
                "ltpt_input": ltpt_input,
                "code_override": code_override or "",
                "name_override": name_override or "",
            })
