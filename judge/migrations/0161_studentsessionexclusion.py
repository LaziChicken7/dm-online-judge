from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0160_tuition_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='StudentSessionExclusion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('note', models.CharField(blank=True, default='', max_length=255, verbose_name='note')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('contest', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='student_exclusions', to='judge.contest', verbose_name='contest')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='session_exclusions', to='judge.organization', verbose_name='organization')),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tuition_exclusions', to='judge.profile', verbose_name='student profile')),
            ],
            options={
                'verbose_name': 'student session exclusion',
                'verbose_name_plural': 'student session exclusions',
                'unique_together': {('organization', 'profile', 'contest')},
            },
        ),
    ]
