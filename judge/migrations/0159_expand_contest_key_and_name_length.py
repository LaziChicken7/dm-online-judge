# Generated manually on 2026-09-23

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0158_expand_problem_code_and_name_length'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contest',
            name='key',
            field=models.CharField(
                max_length=64,
                unique=True,
                validators=[django.core.validators.RegexValidator('^[a-z0-9_]+$', 'Contest id must be ^[a-z0-9_]+$')],
                verbose_name='contest id',
            ),
        ),
        migrations.AlterField(
            model_name='contest',
            name='name',
            field=models.CharField(
                db_index=True,
                max_length=255,
                verbose_name='contest name',
            ),
        ),
    ]
