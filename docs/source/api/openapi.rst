======================
OpenAPI Specification
======================

Shizuha Hub provides an auto-generated OpenAPI 3.0 specification for the REST API.

Interactive Documentation
-------------------------

When running in development mode, interactive API documentation is available at:

- **Swagger UI**: ``http://localhost:8010/shizuha-pulse/api/schema/swagger-ui/``
- **ReDoc**: ``http://localhost:8010/shizuha-pulse/api/schema/redoc/``
- **Raw Schema**: ``http://localhost:8010/shizuha-pulse/api/schema/``

Downloading the Schema
----------------------

Download the OpenAPI schema in various formats:

.. code-block:: bash

    # JSON format
    curl http://localhost:8010/shizuha-pulse/api/schema/ > openapi.json

    # YAML format
    curl http://localhost:8010/shizuha-pulse/api/schema/?format=yaml > openapi.yaml

Using with API Clients
----------------------

Postman
^^^^^^^

1. Open Postman
2. Click "Import"
3. Select "Link" and paste the schema URL
4. Click "Import"

Insomnia
^^^^^^^^

1. Open Insomnia
2. Click "Import/Export"
3. Select "Import Data"
4. Choose "From URL" and paste the schema URL

Code Generation
^^^^^^^^^^^^^^^

Generate client libraries using OpenAPI Generator:

.. code-block:: bash

    # Python client
    openapi-generator-cli generate \
        -i http://localhost:8010/shizuha-pulse/api/schema/ \
        -g python \
        -o ./shizuha-client

    # TypeScript client
    openapi-generator-cli generate \
        -i http://localhost:8010/shizuha-pulse/api/schema/ \
        -g typescript-fetch \
        -o ./shizuha-ts-client

Schema Configuration
--------------------

The OpenAPI schema is generated using drf-spectacular with these settings:

.. code-block:: python

    # In settings.py
    SPECTACULAR_SETTINGS = {
        'TITLE': 'Shizuha Hub API',
        'DESCRIPTION': 'Unified Task, Action, and Alert Management Service',
        'VERSION': '1.0.0',
        'SERVE_INCLUDE_SCHEMA': False,
        'SCHEMA_PATH_PREFIX': '/shizuha-pulse/api',
    }

Adding to Your Django Project
-----------------------------

To enable OpenAPI in your Shizuha Hub deployment:

1. Install drf-spectacular:

   .. code-block:: bash

       pip install drf-spectacular

2. Add to INSTALLED_APPS:

   .. code-block:: python

       INSTALLED_APPS = [
           ...
           'drf_spectacular',
       ]

3. Add to REST_FRAMEWORK settings:

   .. code-block:: python

       REST_FRAMEWORK = {
           ...
           'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
       }

4. Add URL patterns:

   .. code-block:: python

       from drf_spectacular.views import (
           SpectacularAPIView,
           SpectacularRedocView,
           SpectacularSwaggerView
       )

       urlpatterns = [
           path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
           path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema')),
           path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema')),
       ]

Customizing the Schema
----------------------

Add custom schema information to views using decorators:

.. code-block:: python

    from drf_spectacular.utils import extend_schema, OpenApiParameter

    class ItemViewSet(viewsets.ModelViewSet):
        @extend_schema(
            summary="List all items",
            description="Returns paginated list of all items",
            parameters=[
                OpenApiParameter(
                    name='mode',
                    type=str,
                    enum=['task', 'action', 'alert'],
                    description='Filter by item mode'
                ),
            ],
            responses={200: ItemListSerializer(many=True)}
        )
        def list(self, request):
            ...
