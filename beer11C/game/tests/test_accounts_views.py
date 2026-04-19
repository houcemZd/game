"""
Tests for accounts_views: login, register, logout.
"""
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User


class LoginViewTest(TestCase):
    def setUp(self):
        User.objects.create_user('alice', password='securepass99')

    def test_get_200(self):
        r = self.client.get(reverse('login'))
        self.assertEqual(r.status_code, 200)

    def test_valid_login_redirects(self):
        r = self.client.post(reverse('login'), {
            'username': 'alice', 'password': 'securepass99',
        })
        self.assertRedirects(r, reverse('home'))

    def test_invalid_password_shows_error(self):
        r = self.client.post(reverse('login'), {
            'username': 'alice', 'password': 'wrongpass',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Invalid username or password')

    def test_unknown_user_shows_error(self):
        r = self.client.post(reverse('login'), {
            'username': 'nobody', 'password': 'securepass99',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Invalid username or password')

    def test_authenticated_user_redirects(self):
        self.client.login(username='alice', password='securepass99')
        r = self.client.get(reverse('login'))
        self.assertRedirects(r, reverse('home'))

    def test_next_parameter_respected(self):
        r = self.client.post(reverse('login') + '?next=/new/', {
            'username': 'alice', 'password': 'securepass99',
        })
        self.assertRedirects(r, '/new/')


class RegisterViewTest(TestCase):
    def test_get_200(self):
        r = self.client.get(reverse('register'))
        self.assertEqual(r.status_code, 200)

    def test_valid_registration(self):
        r = self.client.post(reverse('register'), {
            'username': 'newuser',
            'email': 'new@example.com',
            'password1': 'StrongPass#9',
            'password2': 'StrongPass#9',
            'first_name': 'New',
        })
        self.assertTrue(User.objects.filter(username='newuser').exists())
        self.assertRedirects(r, reverse('home'))

    def test_valid_registration_logs_in(self):
        self.client.post(reverse('register'), {
            'username': 'bob',
            'email': 'bob@example.com',
            'password1': 'StrongPass#9',
            'password2': 'StrongPass#9',
            'first_name': 'Bob',
        })
        r = self.client.get(reverse('home'))
        self.assertEqual(r.status_code, 200)   # logged in — no redirect

    def test_mismatched_passwords_shows_error(self):
        r = self.client.post(reverse('register'), {
            'username': 'carol',
            'email': '',
            'password1': 'StrongPass#9',
            'password2': 'DifferentPass#1',
            'first_name': '',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'do not match')
        self.assertFalse(User.objects.filter(username='carol').exists())

    def test_empty_username_shows_error(self):
        r = self.client.post(reverse('register'), {
            'username': '',
            'email': '',
            'password1': 'StrongPass#9',
            'password2': 'StrongPass#9',
            'first_name': '',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Username is required')

    def test_duplicate_username_shows_generic_error(self):
        User.objects.create_user('existing', password='StrongPass#9')
        r = self.client.post(reverse('register'), {
            'username': 'existing',
            'email': '',
            'password1': 'StrongPass#9',
            'password2': 'StrongPass#9',
            'first_name': '',
        })
        self.assertEqual(r.status_code, 200)
        # Must NOT say "already taken" — that reveals the username exists.
        self.assertNotContains(r, 'already taken')
        # Should show a generic error message instead.
        self.assertContains(r, 'Registration failed')

    def test_weak_password_rejected(self):
        """Django's password validators reject passwords that are too simple."""
        r = self.client.post(reverse('register'), {
            'username': 'dave',
            'email': '',
            'password1': '12345678',   # numeric-only — rejected by NumericPasswordValidator
            'password2': '12345678',
            'first_name': '',
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username='dave').exists())

    def test_short_password_rejected(self):
        r = self.client.post(reverse('register'), {
            'username': 'eve',
            'email': '',
            'password1': 'Short1!',   # only 7 chars
            'password2': 'Short1!',
            'first_name': '',
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username='eve').exists())

    def test_authenticated_user_redirects(self):
        User.objects.create_user('alice', password='StrongPass#9')
        self.client.login(username='alice', password='StrongPass#9')
        r = self.client.get(reverse('register'))
        self.assertRedirects(r, reverse('home'))


class LogoutViewTest(TestCase):
    def setUp(self):
        User.objects.create_user('alice', password='securepass99')
        self.client.login(username='alice', password='securepass99')

    def test_post_logs_out(self):
        r = self.client.post(reverse('logout'))
        # After logout, should redirect to login
        self.assertRedirects(r, reverse('login'))
        # Subsequent request to home should redirect to login
        r2 = self.client.get(reverse('home'))
        self.assertIn('/accounts/login/', r2['Location'])

    def test_get_not_allowed(self):
        r = self.client.get(reverse('logout'))
        self.assertEqual(r.status_code, 405)
