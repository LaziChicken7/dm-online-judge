from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0161_studentsessionexclusion'),
    ]

    operations = [
        migrations.AddField(
            model_name='contesttuition',
            name='session_count',
            field=models.IntegerField(
                default=1,
                help_text='Number of sessions this contest counts for (e.g. 1, 2, 3)',
                verbose_name='number of sessions (số buổi)',
            ),
        ),
    ]
