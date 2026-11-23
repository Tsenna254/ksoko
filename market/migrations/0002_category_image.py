import market.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('market', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='image',
            field=models.ImageField(blank=True, help_text='Optional picture shown on the home page and category pages. A square picture works best. Leave empty to use the built-in artwork.', upload_to=market.models.category_image_path),
        ),
    ]
