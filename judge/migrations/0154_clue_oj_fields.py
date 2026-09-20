from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0153_problem_is_vjudge_problem_vjudge_oj_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='is_clue',
            field=models.BooleanField(db_index=True, default=False, verbose_name='is ClueOJ problem'),
        ),
        migrations.AddField(
            model_name='problem',
            name='clue_code',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='ClueOJ problem code'),
        ),
        migrations.AddField(
            model_name='profile',
            name='clue_username',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='ClueOJ username'),
        ),
        migrations.AddField(
            model_name='profile',
            name='clue_password',
            field=models.CharField(blank=True, default='', max_length=128, verbose_name='ClueOJ password'),
        ),
        migrations.AddField(
            model_name='profile',
            name='clue_cookie',
            field=models.TextField(blank=True, default='', verbose_name='ClueOJ session cookie'),
        ),
        migrations.AddField(
            model_name='submission',
            name='clue_submission_id',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='ClueOJ submission ID'),
        ),
    ]
