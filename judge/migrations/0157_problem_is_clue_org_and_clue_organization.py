# Generated manually on 2026-09-20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0156_problem_is_ltpt_problem_ltpt_code_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='is_clue_org',
            field=models.BooleanField(db_index=True, default=False, verbose_name='is ClueOJ organization problem'),
        ),
        migrations.AddField(
            model_name='problem',
            name='clue_organization',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='ClueOJ organization name'),
        ),
    ]
