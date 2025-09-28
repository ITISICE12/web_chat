import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Message, Room, PrivateMessage
from django.contrib.auth.models import User
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.contrib.auth import get_user_model

class ChatConsumer(AsyncWebsocketConsumer):
    # Храним подключенных пользователей по комнатам
    connected_users = {}
    
    # Храним channel_name для каждого пользователя для личных сообщений
    user_channels = {}
    
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
        
        # Регистрируем канал пользователя для личных сообщений
        self.user_channels[self.user.username] = self.channel_name
        
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
        
        # Отправляем историю личных сообщений
        await self.send_private_history()

    async def disconnect(self, close_code):
        # Уведомляем всех об отключении пользователя
        if hasattr(self, 'room_group_name') and not self.user.is_anonymous:
            # Удаляем пользователя из списка
            await self.remove_user_from_room()
            
            # Удаляем канал пользователя
            if self.user.username in self.user_channels:
                del self.user_channels[self.user.username]
            
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
            message_type = text_data_json.get('type', 'chat_message')
            
            if message_type == 'private_message':
                await self.handle_private_message(text_data_json)
            else:
                await self.handle_chat_message(text_data_json)
                
        except Exception as e:
            print(f"❌ Ошибка обработки сообщения: {e}")

    async def handle_chat_message(self, text_data_json):
        """Обработка обычных сообщений в чат"""
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

        # Сохраняем сообщение в БД
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
                }
            )
        else:
            print("❌ Не удалось сохранить сообщение, трансляция отменена")

    async def handle_private_message(self, text_data_json):
        """Обработка личных сообщений"""
        message = text_data_json.get('message', '').strip()
        to_username = text_data_json.get('to_username', '')
        from_username = text_data_json.get('username', '')
        
        if not message or not to_username:
            return
            
        print(f"📨 Личное сообщение от {from_username} к {to_username}: {message}")

        # Проверяем, что оба пользователя существуют
        from_user_exists = await self.check_user_exists(from_username)
        to_user_exists = await self.check_user_exists(to_username)
        
        if not from_user_exists or not to_user_exists:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Пользователь не найден'
            }))
            return

        # Сохраняем личное сообщение в БД
        saved_message = await self.save_private_message(from_username, to_username, message)

        if saved_message:
            # Используем текущее время, если timestamp недоступен
            timestamp = getattr(saved_message, 'timestamp', timezone.now())
            
            # Отправляем сообщение отправителю
            await self.send(text_data=json.dumps({
                'type': 'private_message_sent',
                'message': message,
                'to_username': to_username,
                'timestamp': timestamp.isoformat(),
                'message_id': saved_message.id
            }))

            # Отправляем сообщение получателю, если он онлайн
            if to_username in self.user_channels:
                await self.channel_layer.send(
                    self.user_channels[to_username],
                    {
                        'type': 'private_message',
                        'message': message,
                        'from_username': from_username,
                        'timestamp': timestamp.isoformat(),
                        'message_id': saved_message.id
                    }
                )
                print(f"✅ Личное сообщение доставлено пользователю {to_username}")
            else:
                print(f"ℹ️ Пользователь {to_username} не в сети, сообщение сохранено")
        else:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Не удалось отправить личное сообщение'
            }))

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
        }))

    async def private_message(self, event):
        """Обработчик для личных сообщений - отправляет конкретному пользователю"""
        print(f"📤 Доставляем личное сообщение от {event['from_username']}")
        await self.send(text_data=json.dumps({
            'type': 'private_message',
            'message': event['message'],
            'from_username': event['from_username'],
            'timestamp': event.get('timestamp'),
            'message_id': event.get('message_id'),
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

    # МЕТОДЫ ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ

    @sync_to_async
    def save_private_message(self, from_username, to_username, message):
        """Сохраняет личное сообщение в БД"""
        try:
            from_user = get_user_model().objects.get(username=from_username)
            to_user = get_user_model().objects.get(username=to_username)
            
            private_message = PrivateMessage.objects.create(
                from_user=from_user,
                to_user=to_user,
                content=message
            )
            print(f"💾 Личное сообщение сохранено: {from_username} -> {to_username}")
            return private_message
        except Exception as e:
            print(f"❌ Ошибка сохранения личного сообщения: {e}")
            return None

    @sync_to_async
    def get_private_messages(self, username, limit=50):
        """Получает историю личных сообщений пользователя"""
        try:
            User = get_user_model()
            user = User.objects.get(username=username)
            
            # Получаем сообщения где пользователь отправитель или получатель
            sent_messages = PrivateMessage.objects.filter(from_user=user)
            received_messages = PrivateMessage.objects.filter(to_user=user)
            
            # Объединяем и сортируем
            from itertools import chain
            all_messages = sorted(
                chain(sent_messages, received_messages),
                key=lambda x: x.timestamp
            )
            
            messages_list = []
            for msg in all_messages[-limit:]:
                messages_list.append({
                    'id': msg.id,
                    'message': msg.content,
                    'from_username': msg.from_user.username,
                    'to_username': msg.to_user.username,
                    'timestamp': msg.timestamp.isoformat(),
                    'direction': 'sent' if msg.from_user == user else 'received'
                })
            return messages_list
        
        except Exception as e:
            print(f"❌ Ошибка загрузки личной истории: {e}")
            return []

    async def send_private_history(self):
        """Отправляет историю личных сообщений текущему пользователю"""
        messages = await self.get_private_messages(self.user.username)
        await self.send(text_data=json.dumps({
            'type': 'private_history',
            'messages': messages
        }))
        print(f"📚 Отправлено {len(messages)} личных сообщений для {self.user.username}")

    # СУЩЕСТВУЮЩИЕ МЕТОДЫ

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
        
        # Ограничиваем размер буфера (последние 50 сообщений)
        self.message_buffer[self.room_slug].append(message_data)
        if len(self.message_buffer[self.room_slug]) > 50:
            self.message_buffer[self.room_slug] = self.message_buffer[self.room_slug][-50:]
        
        print(f"💾 Сообщение {message_obj.id} добавлено в буфер комнаты {self.room_slug}")

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
        """Получает сообщения из БД"""
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

    async def get_combined_messages(self, room_slug, limit=50):
        """Получает сообщения из БД и объединяет с буфером"""
        try:
            # Получаем сообщения из БД
            db_messages = await self.get_room_messages(room_slug, limit)
            
            # Получаем сообщения из буфера
            buffer_messages = self.message_buffer.get(room_slug, [])
            
            # Объединяем сообщения, убирая дубликаты по ID
            combined_messages = []
            seen_ids = set()
            
            # Сначала добавляем сообщения из буфера (более новые)
            for msg in reversed(buffer_messages):
                if msg['id'] not in seen_ids:
                    combined_messages.append({
                        'id': msg['id'],
                        'message': msg['message'],
                        'username': msg['username'],
                        'date_added': msg['timestamp']
                    })
                    seen_ids.add(msg['id'])
            
            # Затем добавляем сообщения из БД (более старые)
            for msg in db_messages:
                if msg['id'] not in seen_ids:
                    combined_messages.append(msg)
                    seen_ids.add(msg['id'])
            
            # Сортируем по времени (самые старые первыми)
            combined_messages.sort(key=lambda x: x['date_added'] if x['date_added'] else '')
            
            # Ограничиваем лимитом
            return combined_messages[-limit:]
            
        except Exception as e:
            print(f"❌ Ошибка объединения сообщений: {e}")
            return await self.get_room_messages(room_slug, limit)

    async def send_history(self):
        """Отправляет полную историю сообщений (из БД + буфера) только текущему пользователю"""
        messages = await self.get_combined_messages(self.room_slug)
        await self.send(text_data=json.dumps({
            'type': 'history',
            'messages': messages
        }))
        print(f"📚 Отправлено {len(messages)} сообщений из истории для {self.user.username}")