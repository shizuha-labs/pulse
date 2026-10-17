import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taskmanager.settings')

app = Celery('shizuha_pulse')
app.config_from_object('django.conf:settings', namespace='CELERY')

# Configure task routing and naming to avoid conflicts
app.conf.update(
    task_default_queue='shizuha_pulse',
    task_default_exchange='shizuha_pulse',
    task_default_routing_key='shizuha_pulse',
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

app.autodiscover_tasks()

# PLAT-11277: this spot used to OVERWRITE app.conf.broker_transport_options
# after config_from_object had loaded CELERY_BROKER_TRANSPORT_OPTIONS from
# Django settings — silently dropping visibility_timeout and every other key
# at the workers. broker_transport_options now has a single source of truth:
# taskmanager/settings.py. Never assign app.conf.broker_transport_options
# here again; extend the settings dict instead.

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
