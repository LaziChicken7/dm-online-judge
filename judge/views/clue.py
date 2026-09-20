from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _, gettext
from django.views import View
from judge.utils.clue_service import check_clue_login, login_clue
from judge.utils.views import TitleMixin

class ClueConnectView(LoginRequiredMixin, TitleMixin, View):
    title = _("Kết nối tài khoản Clue Online Judge (ClueOJ)")

    def get(self, request):
        profile = request.profile
        cookie = profile.clue_cookie
        is_connected = False
        if cookie:
            check = check_clue_login(cookie)
            is_connected = check.get("logged_in", False)
        elif profile.clue_username and profile.clue_password:
            res = login_clue(profile.clue_username, profile.clue_password)
            if res.get("success"):
                profile.clue_cookie = res["cookie"]
                profile.save(update_fields=["clue_cookie"])
                is_connected = True

        return render(request, "clue/connect.html", {
            "title": self.get_title(),
            "profile": profile,
            "is_connected": is_connected,
            "next_url": request.GET.get("next") or "",
        })

    def post(self, request):
        profile = request.profile
        clue_username = request.POST.get("clue_username", "").strip()
        clue_password = request.POST.get("clue_password", "").strip()

        if not clue_username or not clue_password:
            return render(request, "clue/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": _("Vui lòng nhập đầy đủ tên đăng nhập và mật khẩu ClueOJ."),
                "clue_username": clue_username,
                "next_url": request.GET.get("next") or "",
            })

        res = login_clue(clue_username, clue_password)
        if not res.get("success"):
            return render(request, "clue/connect.html", {
                "title": self.get_title(),
                "profile": profile,
                "error": res.get("error", _("Đăng nhập ClueOJ thất bại. Vui lòng kiểm tra lại tài khoản và mật khẩu.")),
                "clue_username": clue_username,
                "next_url": request.GET.get("next") or "",
            })

        profile.clue_username = clue_username
        profile.clue_password = clue_password
        profile.clue_cookie = res["cookie"]
        profile.save(update_fields=["clue_username", "clue_password", "clue_cookie"])

        next_url = request.GET.get("next") or request.POST.get("next") or reverse("clue_connect")
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("ajax"):
            return JsonResponse({
                "success": True,
                "message": gettext("Đăng nhập và kết nối tài khoản ClueOJ thành công!"),
                "username": clue_username,
            })
        return HttpResponseRedirect(next_url)


class ClueDisconnectView(LoginRequiredMixin, View):
    def post(self, request):
        profile = request.profile
        profile.clue_username = ""
        profile.clue_password = ""
        profile.clue_cookie = ""
        profile.save(update_fields=["clue_username", "clue_password", "clue_cookie"])
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": True, "message": gettext("Đã ngắt kết nối tài khoản ClueOJ.")})
        return HttpResponseRedirect(reverse("clue_connect"))
