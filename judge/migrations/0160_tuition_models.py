from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0159_expand_contest_key_and_name_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationTuition',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('billing_type', models.CharField(choices=[('individual', 'Dạy kèm riêng / Tính theo người đi học'), ('center', 'Trung tâm / Tính cố định cả khi nghỉ')], default='individual', max_length=20, verbose_name='billing type')),
                ('default_fee_per_session', models.IntegerField(default=200000, verbose_name='default fee per session (VND)')),
                ('note', models.TextField(blank=True, default='', verbose_name='note')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='tuition_setting', to='judge.organization', verbose_name='organization')),
            ],
            options={
                'verbose_name': 'organization tuition setting',
                'verbose_name_plural': 'organization tuition settings',
            },
        ),
        migrations.CreateModel(
            name='ContestTuition',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fee_override', models.IntegerField(blank=True, help_text='Leave empty to use organization default fee', null=True, verbose_name='custom fee for this session (VND)')),
                ('is_billed', models.BooleanField(default=True, verbose_name='is billed session?')),
                ('note', models.CharField(blank=True, default='', max_length=255, verbose_name='note')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contest', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='tuition_info', to='judge.contest', verbose_name='contest')),
            ],
            options={
                'verbose_name': 'contest tuition',
                'verbose_name_plural': 'contest tuitions',
            },
        ),
        migrations.CreateModel(
            name='StudentTuitionPayment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period', models.CharField(default='all', max_length=20, verbose_name='billing period (e.g. 2026-09 or all)')),
                ('custom_fee_per_session', models.IntegerField(blank=True, null=True, verbose_name='custom fee per session for this student')),
                ('paid_amount', models.IntegerField(default=0, verbose_name='paid amount (VND)')),
                ('status', models.CharField(choices=[('unpaid', 'Chưa thanh toán'), ('partial', 'Đã thanh toán một phần'), ('paid', 'Đã thanh toán')], default='unpaid', max_length=20, verbose_name='payment status')),
                ('note', models.TextField(blank=True, default='', verbose_name='note')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tuition_payments', to='judge.organization', verbose_name='organization')),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tuition_records', to='judge.profile', verbose_name='student profile')),
            ],
            options={
                'verbose_name': 'student tuition record',
                'verbose_name_plural': 'student tuition records',
                'unique_together': {('organization', 'profile', 'period')},
            },
        ),
    ]
