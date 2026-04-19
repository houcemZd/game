"""
accounts/views.py — Beer Game authentication views
Login · Register · Logout
"""
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST
from django.contrib import messages


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect(request.GET.get('next', 'home'))
        messages.error(request, 'Invalid username or password.')

    return render(request, 'accounts/login.html')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        username   = request.POST.get('username', '').strip()
        email      = request.POST.get('email', '').strip()
        password1  = request.POST.get('password1', '')
        password2  = request.POST.get('password2', '')
        first_name = request.POST.get('first_name', '').strip()

        errors = []
        if not username:
            errors.append("Username is required.")
        elif User.objects.filter(username=username).exists():
            # Use a generic message to avoid revealing which usernames exist.
            errors.append("Registration failed. Please try a different username or check your details.")

        if password1 != password2:
            errors.append("Passwords do not match.")
        else:
            # Run Django's built-in password validators (length, common, numeric, similarity).
            try:
                validate_password(password1)
            except ValidationError as exc:
                errors.extend(exc.messages)

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            user = User.objects.create_user(
                username=username, email=email,
                password=password1, first_name=first_name,
            )
            login(request, user)
            messages.success(request, f"Welcome, {first_name or username}!")
            return redirect('home')

    return render(request, 'accounts/register.html')


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')
