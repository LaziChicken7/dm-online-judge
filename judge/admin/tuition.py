from django.contrib import admin
from judge.models.tuition import ContestTuition, OrganizationTuition, StudentTuitionPayment, StudentSessionExclusion


class OrganizationTuitionAdmin(admin.ModelAdmin):
    list_display = ('organization', 'billing_type', 'default_fee_per_session', 'updated_at')
    list_filter = ('billing_type',)
    search_fields = ('organization__name', 'note')


class ContestTuitionAdmin(admin.ModelAdmin):
    list_display = ('contest', 'fee_override', 'is_billed', 'updated_at')
    list_filter = ('is_billed',)
    search_fields = ('contest__name', 'contest__key', 'note')


class StudentTuitionPaymentAdmin(admin.ModelAdmin):
    list_display = ('profile', 'organization', 'period', 'custom_fee_per_session', 'paid_amount', 'status', 'updated_at')
    list_filter = ('status', 'period', 'organization')
    search_fields = ('profile__user__username', 'organization__name', 'note')


class StudentSessionExclusionAdmin(admin.ModelAdmin):
    list_display = ('profile', 'contest', 'organization', 'created_at')
    list_filter = ('organization',)
    search_fields = ('profile__user__username', 'contest__name', 'contest__key')
