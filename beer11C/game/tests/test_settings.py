import os
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from beer_game import settings as project_settings


class AllowedHostsSettingsTest(SimpleTestCase):
    def test_defaults_when_no_environment_hosts(self):
        with patch.dict(os.environ, {'ALLOWED_HOSTS': '', 'RAILWAY_PUBLIC_DOMAIN': ''}):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(hosts, ['localhost', '127.0.0.1'])

    def test_includes_configured_allowed_hosts(self):
        with patch.dict(
            os.environ,
            {'ALLOWED_HOSTS': 'example.com, www.example.com', 'RAILWAY_PUBLIC_DOMAIN': ''},
        ):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(hosts, ['localhost', '127.0.0.1', 'example.com', 'www.example.com'])

    def test_includes_railway_public_domain_when_allowed_hosts_unset(self):
        with patch.dict(
            os.environ,
            {'ALLOWED_HOSTS': '', 'RAILWAY_PUBLIC_DOMAIN': 'game-production-a2bc.up.railway.app'},
        ):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(
            hosts,
            ['localhost', '127.0.0.1', 'game-production-a2bc.up.railway.app'],
        )

    def test_railway_domain_deduplicated_with_allowed_hosts(self):
        with patch.dict(
            os.environ,
            {
                'ALLOWED_HOSTS': 'game-production-a2bc.up.railway.app',
                'RAILWAY_PUBLIC_DOMAIN': 'game-production-a2bc.up.railway.app',
            },
        ):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(
            hosts,
            ['localhost', '127.0.0.1', 'game-production-a2bc.up.railway.app'],
        )

    def test_normalizes_url_and_port_hosts(self):
        with patch.dict(
            os.environ,
            {
                'ALLOWED_HOSTS': 'https://example.com, api.example.com:443',
                'RAILWAY_PUBLIC_DOMAIN': 'https://game-production-a2bc.up.railway.app',
            },
        ):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(
            hosts,
            [
                'localhost',
                '127.0.0.1',
                'example.com',
                'api.example.com',
                'game-production-a2bc.up.railway.app',
            ],
        )

    def test_normalizes_quoted_hosts(self):
        with patch.dict(
            os.environ,
            {
                'ALLOWED_HOSTS': '"example.com", \'www.example.com\', "https://api.example.com:443"',
                'RAILWAY_PUBLIC_DOMAIN': '"game-production-a2bc.up.railway.app"',
            },
        ):
            hosts = project_settings._build_allowed_hosts()
        self.assertEqual(
            hosts,
            [
                'localhost',
                '127.0.0.1',
                'example.com',
                'www.example.com',
                'api.example.com',
                'game-production-a2bc.up.railway.app',
            ],
        )


class ProductionConfigSettingsTest(SimpleTestCase):
    def test_requires_database_url_in_production(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'DATABASE_URL is required when DEBUG=False (production).',
        ):
            project_settings._validate_production_services(
                debug=False,
                database_url='',
                redis_url='redis://localhost:6379/0',
            )

    def test_requires_redis_url_in_production(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'REDIS_URL is required when DEBUG=False (production).',
        ):
            project_settings._validate_production_services(
                debug=False,
                database_url='postgres://user:pass@localhost:5432/db',
                redis_url='',
            )

    def test_allows_missing_urls_in_debug(self):
        project_settings._validate_production_services(
            debug=True,
            database_url='',
            redis_url='',
        )
