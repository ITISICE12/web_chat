from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.forms import User

class SingUpForm(UserCreationForm):
    class Meta:
        model = User
        fields = ['username', 'password1', 'password2']