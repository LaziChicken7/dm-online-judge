import logging
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _, gettext
from django.views import View

from judge.utils.ntucoder_importer import import_ntucoder_problem
from judge.utils.ntucoder_service import check_ntucoder_login, login_ntucoder
from judge.utils.views import TitleMixin

logger = logging.getLogger('judge.ntucoder_views')

class NTUCoderConnectView(LoginRequiredMixin, TitleMixin, View):
    title = _("Kết nối tài khoản THPT Chuyên NTUCoder")

    def get(self, request):
        profile = request.profile
        cookie = profile.ntucoder_cookie
        is_connected = False

        if cookie:
            check = check_ntucoder_login(cookie)
            is_connected = check.get("logged_in", False)
            if is_connected and not profile.ntucoder_username and check.get("username"):
                profile.ntucoder_username = check["username"]
                profile.save(update_fields=["ntucoder_username"])
        elif profile.ntucoder_email and profile.ntucoder_password:
            res = login_ntucoder(profile.ntucoder_email, profile.ntucoder_password)
            if res.get("success"):
                profile.ntucoder_cookie = res["cookie"]
                profile.ntucoder_username = res.get("username") or profile.ntucoder_username
                profile.save(update_fields=["ntucoder_cookie", "ntucoder_username"])
                is_connected = True

        return render(request, "ntucoder/connect.html", {
            "title": self.get_title(),
            "profile": profile,
            "is_connected": is_connected,
            "next_url": request.GET.get("next") or "",
            "ntucoder_email": profile.ntucoder_email or "",
        })

    def post(self, request):
        profile = request.profile
        ntucoder_email = request.POST.get("ntucoder_email", "").strip()
        ntucoder_password = request.POST.get("ntucoder_password", "").strip()
        next_url = request.GET.get("next") or request.POST.get("next") or ""

        if not ntucoder_email or not ntucoder_password:
            return render(request, "ntucoder/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": _("Vui lòng nhập đầy đủ Email / Tên đăng nhập và Mật khẩu NTUCoder."),
                "ntucoder_email": ntucoder_email,
                "next_url": next_url,
            })

        res = login_ntucoder(ntucoder_email, ntucoder_password)
        if not res.get("success"):
            return render(request, "ntucoder/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": res.get("error", _("Đăng nhập NTUCoder thất bại. Vui lòng kiểm tra lại tài khoản và mật khẩu.")),
                "ntucoder_email": ntucoder_email,
                "next_url": next_url,
            })

        username = res.get("username") or ntucoder_email.split('@')[0]
        profile.ntucoder_email = ntucoder_email
        profile.ntucoder_password = ntucoder_password
        profile.ntucoder_cookie = res["cookie"]
        profile.ntucoder_username = username
        profile.save(update_fields=["ntucoder_email", "ntucoder_password", "ntucoder_cookie", "ntucoder_username"])

        if not next_url:
            next_url = reverse("ntucoder_connect")

        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax"):
            return JsonResponse({
                "success": True,
                "message": gettext("Kết nối tài khoản NTUCoder thành công!"),
                "username": username,
            })

        return HttpResponseRedirect(next_url)

class NTUCoderDisconnectView(LoginRequiredMixin, View):
    def post(self, request):
        profile = request.profile
        profile.ntucoder_cookie = ""
        profile.ntucoder_password = ""
        profile.ntucoder_username = ""
        profile.save(update_fields=["ntucoder_cookie", "ntucoder_password", "ntucoder_username"])

        next_url = request.GET.get("next") or request.POST.get("next") or reverse("ntucoder_connect")
        return HttpResponseRedirect(next_url)

class ProblemImportNTUCoderView(LoginRequiredMixin, TitleMixin, View):
    title = _("Nhập bài tập từ THPT Chuyên NTUCoder")

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

        return render(request, "problem/import_ntucoder.html", {
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

        ntucoder_input = request.POST.get("ntucoder_input", "").strip()
        code_override = request.POST.get("code_override", "").strip() or None
        name_override = request.POST.get("name_override", "").strip() or None
        points_override = request.POST.get("points_override", "").strip() or None
        time_limit_override = request.POST.get("time_limit_override", "").strip() or None
        memory_limit_override = request.POST.get("memory_limit_override", "").strip() or None
        is_public = request.POST.get("is_public") == "on"

        if not ntucoder_input:
            return render(request, "problem/import_ntucoder.html", {
                "title": self.get_title(),
                "error": _("Vui lòng nhập mã bài hoặc liên kết bài tập từ NTUCoder (ví dụ: 11042 hoặc https://thptchuyen.ntucoder.net/Problem/Details/11042)."),
                "ntucoder_input": ntucoder_input,
                "code_override": code_override or "",
                "name_override": name_override or "",
            })

        try:
            problem = import_ntucoder_problem(
                ntucoder_input=ntucoder_input,
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
            logger.exception(f"ProblemImportNTUCoder error: {e}")
            return render(request, "problem/import_ntucoder.html", {
                "title": self.get_title(),
                "error": str(e),
                "ntucoder_input": ntucoder_input,
                "code_override": code_override or "",
                "name_override": name_override or "",
            })
