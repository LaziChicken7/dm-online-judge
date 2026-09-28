from django.db import models
from django.utils.translation import gettext_lazy as _
from judge.models.profile import Organization, Profile


class OrganizationTuition(models.Model):
    BILLING_CHOICES = (
        ('individual', _('Dạy kèm riêng / Tính theo người đi học')),
        ('center', _('Trung tâm / Tính cố định cả khi nghỉ')),
    )

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name='tuition_setting',
        verbose_name=_('organization'),
    )
    billing_type = models.CharField(
        max_length=20,
        choices=BILLING_CHOICES,
        default='individual',
        verbose_name=_('billing type'),
    )
    default_fee_per_session = models.IntegerField(
        default=200000,
        verbose_name=_('default fee per session (VND)'),
    )
    note = models.TextField(blank=True, default='', verbose_name=_('note'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('organization tuition setting')
        verbose_name_plural = _('organization tuition settings')

    def __str__(self):
        return f"{self.organization.name} ({self.get_billing_type_display()} - {self.default_fee_per_session:,}đ)"


class ContestTuition(models.Model):
    contest = models.OneToOneField(
        'judge.Contest',
        on_delete=models.CASCADE,
        related_name='tuition_info',
        verbose_name=_('contest'),
    )
    session_count = models.IntegerField(
        default=1,
        verbose_name=_('number of sessions (số buổi)'),
        help_text=_('Number of sessions this contest counts for (e.g. 1, 2, 3)'),
    )
    fee_override = models.IntegerField(
        null=True,
        blank=True,
        verbose_name=_('custom fee for this session (VND)'),
        help_text=_('Leave empty to use organization default fee'),
    )
    is_billed = models.BooleanField(
        default=True,
        verbose_name=_('is billed session?'),
    )
    note = models.CharField(max_length=255, blank=True, default='', verbose_name=_('note'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('contest tuition')
        verbose_name_plural = _('contest tuitions')

    def __str__(self):
        return f"{self.contest.name} ({self.fee_override or 'default'}đ)"


class StudentTuitionPayment(models.Model):
    STATUS_CHOICES = (
        ('unpaid', _('Chưa thanh toán')),
        ('partial', _('Đã thanh toán một phần')),
        ('paid', _('Đã thanh toán')),
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='tuition_payments',
        verbose_name=_('organization'),
    )
    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name='tuition_records',
        verbose_name=_('student profile'),
    )
    period = models.CharField(
        max_length=20,
        default='all',
        verbose_name=_('billing period (e.g. 2026-09 or all)'),
    )
    custom_fee_per_session = models.IntegerField(
        null=True,
        blank=True,
        verbose_name=_('custom fee per session for this student'),
    )
    paid_amount = models.IntegerField(
        default=0,
        verbose_name=_('paid amount (VND)'),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='unpaid',
        verbose_name=_('payment status'),
    )
    note = models.TextField(blank=True, default='', verbose_name=_('note'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('organization', 'profile', 'period')
        verbose_name = _('student tuition record')
        verbose_name_plural = _('student tuition records')

    def __str__(self):
        return f"{self.profile.user.username} - {self.organization.name} ({self.status})"


class StudentSessionExclusion(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='session_exclusions',
        verbose_name=_('organization'),
    )
    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name='tuition_exclusions',
        verbose_name=_('student profile'),
    )
    contest = models.ForeignKey(
        'judge.Contest',
        on_delete=models.CASCADE,
        related_name='student_exclusions',
        verbose_name=_('contest'),
    )
    note = models.CharField(max_length=255, blank=True, default='', verbose_name=_('note'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('organization', 'profile', 'contest')
        verbose_name = _('student session exclusion')
        verbose_name_plural = _('student session exclusions')

    def __str__(self):
        return f"Exclusion: {self.profile.user.username} in {self.contest.name} ({self.organization.name})"
