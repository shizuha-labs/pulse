from django.core.management.base import BaseCommand
from django.conf import settings
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import os


class Command(BaseCommand):
    help = 'Create the database if it does not exist'

    def handle(self, *args, **options):
        db_settings = settings.DATABASES['default']
        
        try:
            # Connect to PostgreSQL server (using postgres database)
            conn = psycopg2.connect(
                host=db_settings['HOST'],
                port=db_settings['PORT'],
                user=db_settings['USER'],
                password=db_settings['PASSWORD'],
                database='postgres'  # Connect to default postgres database
            )
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            
            cursor = conn.cursor()
            db_name = db_settings['NAME']
            
            # Check if database exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cursor.fetchone()
            
            if not exists:
                cursor.execute(f'CREATE DATABASE "{db_name}"')
                self.stdout.write(
                    self.style.SUCCESS(f'Successfully created database "{db_name}"')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'Database "{db_name}" already exists')
                )
            
            cursor.close()
            conn.close()
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error creating database: {e}')
            )
            raise e
