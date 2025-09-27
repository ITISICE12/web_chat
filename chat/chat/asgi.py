import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import room.routing  # импортируйте routing вашего приложения

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'chat.settings')

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            room.routing.websocket_urlpatterns  # ← ваши WebSocket URLs
        )
    ),
})