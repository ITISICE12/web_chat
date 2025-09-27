from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import Room, Message
import json

def rooms(request):
    """Страница со списком всех комнат"""
    rooms = Room.objects.all()
    return render(request, 'room/rooms.html', {
        'rooms': rooms
    })

def room(request, slug):
    """Страница конкретной комнаты"""
    room = get_object_or_404(Room, slug=slug)
    
    # Получаем сообщения для комнаты (только из БД)
    messages = Message.objects.filter(room=room).select_related('user').order_by('date_added')[:50]
    
    # Конвертируем сообщения в JSON для передачи в JavaScript
    messages_json = json.dumps([
        {
            'user': {
                'username': msg.user.username,
            },
            'content': msg.content,
            'timestamp': msg.date_added.isoformat() if msg.date_added else None
        }
        for msg in messages
    ])
    
    return render(request, 'room/room.html', {
        'room': room,
        'messages': messages,
        'messages_json': messages_json,
    })