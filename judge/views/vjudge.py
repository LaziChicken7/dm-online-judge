from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _, gettext
from django.views import View

from judge.utils.vjudge_service import check_vjudge_login, get_vjudge_remote_accounts, login_vjudge, normalize_vjudge_cookie
from judge.utils.views import TitleMixin


class VJudgeConnectView(LoginRequiredMixin, TitleMixin, View):
    title = _("Kết nối tài khoản Virtual Judge (VJudge)")

    def get(self, request):
        profile = request.profile
        cookie = profile.vjudge_cookie
        remote_accounts = []
        is_connected = False
        if cookie:
            check = check_vjudge_login(cookie)
            is_connected = check.get("logged_in", False)
            if is_connected:
                remote_accounts = get_vjudge_remote_accounts(cookie, oj="CodeForces")

        return render(request, "vjudge/connect.html", {
            "title": self.get_title(),
            "profile": profile,
            "is_connected": is_connected,
            "remote_accounts": remote_accounts,
        })

    def post(self, request):
        profile = request.profile
        if 'update_vjudge_username' in request.POST:
            new_name = request.POST.get('update_vjudge_username', '').strip()
            if new_name:
                profile.vjudge_username = new_name
                profile.save(update_fields=['vjudge_username'])
            next_url = request.GET.get('next') or reverse('vjudge_connect')
            return HttpResponseRedirect(next_url)
        vjudge_username = request.POST.get("vjudge_username", "").strip()
        vjudge_password = request.POST.get("vjudge_password", "").strip()
        vjudge_cookie = request.POST.get("vjudge_cookie", "").strip()

        # 1. Automatic Login via Username & Password
        if vjudge_password:
            if not vjudge_username:
                return render(request, "vjudge/connect.html", {
                    "title": self.get_title(),
                    "profile": profile,
                    "error": _("Vui lòng nhập tên đăng nhập Virtual Judge."),
                    "vjudge_username": vjudge_username,
                })

            res = login_vjudge(vjudge_username, vjudge_password)
            if not res.get("success"):
                return render(request, "vjudge/connect.html", {
                    "title": self.get_title(),
                    "profile": profile,
                    "error": res.get("error", _("Đăng nhập thất bại. Vui lòng kiểm tra lại tài khoản và mật khẩu.")),
                    "vjudge_username": vjudge_username,
                })

            # Save credentials & cookie
            profile.vjudge_username = vjudge_username
            profile.vjudge_cookie = res["cookie"]
            profile.save(update_fields=["vjudge_username", "vjudge_cookie"])

            next_url = request.GET.get("next") or reverse("vjudge_connect")
            if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax"):
                remote_accounts = get_vjudge_remote_accounts(res["cookie"], oj="CodeForces")
                return JsonResponse({
                    "success": True,
                    "message": gettext("Đăng nhập và kết nối tài khoản Virtual Judge thành công!"),
                    "username": vjudge_username,
                    "remote_accounts": remote_accounts,
                })

            return HttpResponseRedirect(next_url)

                # 2. Fallback via Cookie
        if vjudge_cookie:
            vjudge_cookie = normalize_vjudge_cookie(vjudge_cookie)
            check = check_vjudge_login(vjudge_cookie)
            if not check.get("logged_in"):
                return render(request, "vjudge/connect.html", {
                    "title": self.get_title(),
                    "profile": profile,
                    "error": _("Cookie không hợp lệ hoặc phiên đăng nhập đã hết hạn trên VJudge. Vui lòng kiểm tra lại."),
                    "vjudge_username": vjudge_username,
                    "vjudge_cookie": vjudge_cookie,
                    "active_tab": "cookie",
                })

            if not vjudge_username:
                if check.get("raw", "").startswith("{"):
                    try:
                        import json
                        vjudge_username = json.loads(check["raw"]).get("username", "")
                    except Exception:
                        pass
                if not vjudge_username:
                    vjudge_username = profile.user.username or "vjudge_user"

            profile.vjudge_username = vjudge_username
            profile.vjudge_cookie = vjudge_cookie
            profile.save(update_fields=["vjudge_username", "vjudge_cookie"])

            next_url = request.GET.get("next") or reverse("vjudge_connect")
            if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax"):
                remote_accounts = get_vjudge_remote_accounts(vjudge_cookie, oj="CodeForces")
                return JsonResponse({
                    "success": True,
                    "message": gettext("Kết nối tài khoản Virtual Judge bằng Cookie thành công!"),
                    "username": vjudge_username,
                    "remote_accounts": remote_accounts,
                })

            return HttpResponseRedirect(next_url)

        return render(request, "vjudge/connect.html", {
            "title": self.get_title(),
            "profile": profile,
            "error": _("Vui lòng nhập tên đăng nhập và mật khẩu Virtual Judge."),
            "vjudge_username": vjudge_username,
        })


class VJudgeDisconnectView(LoginRequiredMixin, View):
    def post(self, request):
        profile = request.profile
        profile.vjudge_username = ""
        profile.vjudge_cookie = ""
        profile.vjudge_binding_id = None
        profile.save(update_fields=["vjudge_username", "vjudge_cookie", "vjudge_binding_id"])

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": True, "message": gettext("Đã ngắt kết nối tài khoản VJudge.")})

        return HttpResponseRedirect(reverse("vjudge_connect"))


class VJudgeRemoteAccountsApi(LoginRequiredMixin, View):
    def get(self, request):
        oj = request.GET.get("oj", "CodeForces")
        profile = request.profile
        if not profile.vjudge_cookie:
            return JsonResponse({
                "connected": False,
                "accounts": [],
                "error": gettext("Chưa kết nối tài khoản VJudge"),
            })

        accounts = get_vjudge_remote_accounts(profile.vjudge_cookie, oj=oj)
        return JsonResponse({
            "connected": True,
            "username": profile.vjudge_username,
            "accounts": accounts,
        })
