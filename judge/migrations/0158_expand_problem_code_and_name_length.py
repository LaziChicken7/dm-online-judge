# Generated manually on 2026-09-23

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0157_problem_is_clue_org_and_clue_organization'),
    ]

    operations = [
        migrations.AlterField(
            model_name='problem',
            name='code',
            field=models.CharField(
                db_index=True,
                help_text='A short, unique code for the problem, used in the URL after /problem/',
                max_length=100,
                unique=True,
                validators=[django.core.validators.RegexValidator('^[a-z0-9_]+$', 'Problem code must be ^[a-z0-9_]+$')],
                verbose_name='problem code',
            ),
        ),
        migrations.AlterField(
            model_name='problem',
            name='name',
            field=models.CharField(
                db_index=True,
                help_text='The full name of the problem, as shown in the problem list.',
                max_length=255,
                verbose_name='problem name',
            ),
        ),
    ]
