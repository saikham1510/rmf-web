from api_server.app_config import app_config

# Minimal Tortoise ORM config for Aerich without importing the full app
TORTOISE_ORM = app_config.get_tortoise_orm_config()
