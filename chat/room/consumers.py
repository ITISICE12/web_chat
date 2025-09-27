import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Message, Room
from django.contrib.auth.models import User
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.contrib.auth import get_user_model

class ChatConsumer(AsyncWebsocketConsumer):
    # Храним подключенных пользователей по комнатам
    connected_users = {}
    
    # Буфер для временного хранения сообщений во время переподключения
    message_buffer = {}
    
    async def connect(self):
        self.room_slug = self.scope['url_route']['kwargs']['room_slug']
        self.room_group_name = f'chat_{self.room_slug}'
        self.user = self.scope["user"]

        # Проверяем аутентификацию пользователя
        if self.user.is_anonymous:
            print("❌ Анонимный пользователь пытается подключиться")
            await self.close()
            return

        # Проверяем существование комнаты
        room_exists = await self.check_room_exists(self.room_slug)
        if not room_exists:
            print(f"❌ Комната {self.room_slug} не существует")
            await self.close()
            return

        # Присоединяемся к группе комнаты
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        print(f"✅ WebSocket подключен: {self.user.username} к комнате {self.room_slug}")
        
        # Добавляем пользователя в список подключенных
        await self.add_user_to_room()
        
        # Отправляем обновленный список пользователей ВСЕМ участникам
        await self.send_online_users()
        
        # Уведомляем всех о подключении нового пользователя
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'user_joined',
                'username': self.user.username,
                'timestamp': timezone.now().isoformat()
            }
        )
        
        # Отправляем историю сообщений только подключившемуся пользователю
        await self.send_history()

    async def disconnect(self, close_code):
        # Уведомляем всех об отключении пользователя
        if hasattr(self, 'room_group_name') and not self.user.is_anonymous:
            # Удаляем пользователя из списка
            await self.remove_user_from_room()
            
            # Отправляем обновленный список пользователей
            await self.send_online_users()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'user_left',
                    'username': self.user.username,
                    'timestamp': timezone.now().isoformat()
                }
            )
            
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
        
        print(f"❌ WebSocket отключен: {self.user.username if not self.user.is_anonymous else 'Anonymous'}")

    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            message = text_data_json.get('message', '').strip()
            username = text_data_json.get('username', '')
            
            if not message:
                return

            print(f"📨 Сообщение от {username}: {message}")

            # Проверяем, что пользователь существует
            user_exists = await self.check_user_exists(username)
            if not user_exists:
                print(f"❌ Пользователь {username} не существует")
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Пользователь не найден'
                }))
                return

            # Сохраняем сообщение
            saved_message = await self.save_message(username, self.room_slug, message)

            if saved_message:
                # Добавляем сообщение в буфер
                await self.add_message_to_buffer(saved_message)
                
                # Отправляем сообщение ВСЕМ участникам комнаты
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'chat_message',
                        'message': message,
                        'username': username,
                        'message_id': saved_message.id,
                        'timestamp': saved_message.date_added.isoformat() if saved_message.date_added else timezone.now().isoformat(),
                        'is_buffered': False  # Добавляем поле is_buffered
                    }
                )
            else:
                print("❌ Не удалось сохранить сообщение, трансляция отменена")
                
        except Exception as e:
            print(f"❌ Ошибка обработки сообщения: {e}")

    # НОВЫЙ МЕТОД ДЛЯ БУФЕРИЗАЦИИ СООБЩЕНИЙ
    async def add_message_to_buffer(self, message_obj):
        """Добавляет сообщение в буфер для всех пользователей комнаты"""
        if self.room_slug not in self.message_buffer:
            self.message_buffer[self.room_slug] = []
        
        message_data = {
            'id': message_obj.id,
            'message': message_obj.content,
            'username': message_obj.user.username,
            'timestamp': message_obj.date_added.isoformat() if message_obj.date_added else timezone.now().isoformat()
        }
        
        # Ограничиваем размер буфера (последние 20 сообщений)
        self.message_buffer[self.room_slug].append(message_data)
        if len(self.message_buffer[self.room_slug]) > 20:
            self.message_buffer[self.room_slug] = self.message_buffer[self.room_slug][-20:]
        
        print(f"💾 Сообщение {message_obj.id} добавлено в буфер комнаты {self.room_slug}")

    # ОБРАБОТЧИКИ ДЛЯ РАЗНЫХ ТИПОВ СООБЩЕНИЙ:

    async def chat_message(self, event):
        """Обработчик для чат-сообщений - отправляет ВСЕМ участникам"""
        print(f"📤 Транслируем сообщение от {event['username']} всем участникам")
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': event['message'],
            'username': event['username'],
            'message_id': event.get('message_id'),
            'timestamp': event.get('timestamp'),
            'is_buffered': event.get('is_buffered', False)  # Добавляем поле is_buffered
        }))

    async def user_joined(self, event):
        """Обработчик для уведомлений о входе пользователя"""
        print(f"👤 Пользователь {event['username']} присоединился")
        await self.send(text_data=json.dumps({
            'type': 'user_activity',
            'activity': 'joined',
            'username': event['username'],
            'timestamp': event.get('timestamp'),
            'message': f"{event['username']} присоединился к чату"
        }))

    async def user_left(self, event):
        """Обработчик для уведомлений о выходе пользователя"""
        print(f"👤 Пользователь {event['username']} покинул чат")
        await self.send(text_data=json.dumps({
            'type': 'user_activity',
            'activity': 'left',
            'username': event['username'],
            'timestamp': event.get('timestamp'),
            'message': f"{event['username']} покинул чат"
        }))

    async def online_users(self, event):
        """Обработчик для отправки списка онлайн пользователей"""
        await self.send(text_data=json.dumps({
            'type': 'online_users',
            'users': event['users'],
            'count': event['count']
        }))

    # МЕТОДЫ ДЛЯ УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ ОНЛАЙН

    async def add_user_to_room(self):
        """Добавляет пользователя в список подключенных к комнате"""
        if self.room_slug not in self.connected_users:
            self.connected_users[self.room_slug] = set()
        
        self.connected_users[self.room_slug].add(self.user.username)
        print(f"👥 Пользователь {self.user.username} добавлен в комнату {self.room_slug}")

    async def remove_user_from_room(self):
        """Удаляет пользователя из списка подключенных к комнате"""
        if self.room_slug in self.connected_users:
            if self.user.username in self.connected_users[self.room_slug]:
                self.connected_users[self.room_slug].remove(self.user.username)
                print(f"👥 Пользователь {self.user.username} удален из комнаты {self.room_slug}")
                
                # Если комната пуста, удаляем ее (но буфер сообщений сохраняем)
                if len(self.connected_users[self.room_slug]) == 0:
                    del self.connected_users[self.room_slug]

    async def send_online_users(self):
        """Отправляет обновленный список онлайн пользователей ВСЕМ в комнате"""
        if self.room_slug in self.connected_users:
            users = list(self.connected_users[self.room_slug])
            users_count = len(users)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'online_users',
                    'users': users,
                    'count': users_count
                }
            )
            print(f"👥 Отправлен список пользователей: {users}")

    @sync_to_async
    def check_user_exists(self, username):
        """Проверяет существование пользователя"""
        return get_user_model().objects.filter(username=username).exists()

    @sync_to_async
    def check_room_exists(self, room_slug):
        """Проверяет существование комнаты"""
        return Room.objects.filter(slug=room_slug).exists()

    @sync_to_async  
    def save_message(self, username, room_slug, message):  
        try:
            user = get_user_model().objects.get(username=username)
            room = Room.objects.get(slug=room_slug)  

            message_obj = Message.objects.create(user=user, room=room, content=message)
            print(f"💾 Сообщение сохранено: {username} в комнате {room_slug} (ID: {message_obj.id})")
            return message_obj
        except get_user_model().DoesNotExist:
            print(f"❌ Пользователь {username} не найден в БД")
            return None
        except Room.DoesNotExist:
            print(f"❌ Комната {room_slug} не найдена")
            return None
        except Exception as e:
            print(f"❌ Ошибка сохранения сообщения: {e}")
            return None

    @sync_to_async
    def get_room_messages(self, room_slug, limit=50):
        try:
            room = Room.objects.get(slug=room_slug)
            messages = Message.objects.filter(room=room).select_related('user').order_by('date_added')[:limit]
            
            messages_list = []
            for msg in messages:
                messages_list.append({
                    'id': msg.id,
                    'message': msg.content,
                    'username': msg.user.username,
                    'date_added': msg.date_added.isoformat() if msg.date_added else None
                })
            return messages_list
        except Exception as e:
            print(f"❌ Ошибка загрузки истории: {e}")
            return []

    async def send_history(self):
        """Отправляет историю сообщений только текущему пользователю"""
        messages = await self.get_room_messages(self.room_slug)
        await self.send(text_data=json.dumps({
            'type': 'history',
            'messages': messages
        }))
        print(f"📚 Отправлено {len(messages)} сообщений из истории для {self.user.username}")