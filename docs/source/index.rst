.. Shizuha Hub documentation master file

===================================
Shizuha Hub Documentation
===================================

**Shizuha Hub** is a unified task, action, and alert management service built with Django REST Framework.

.. grid:: 2
    :gutter: 3

    .. grid-item-card:: Getting Started
        :link: guides/quickstart
        :link-type: doc

        New to Shizuha Hub? Start here for installation and basic usage.

    .. grid-item-card:: API Reference
        :link: api/index
        :link-type: doc

        Complete REST API documentation with examples.

    .. grid-item-card:: Models
        :link: models/index
        :link-type: doc

        Database models and their relationships.

    .. grid-item-card:: Services
        :link: services/index
        :link-type: doc

        Business logic services for deduplication, escalation, and more.

Key Features
------------

.. list-table::
   :widths: 25 75
   :header-rows: 0

   * - **Organizations & Projects**
     - Multi-tenant team collaboration with Jira-style projects and item keys
   * - **Unified Model**
     - Single ``Item`` model handles tasks, actions, and alerts with mode-specific behavior
   * - **Alert Deduplication**
     - Fingerprint-based deduplication prevents duplicate alerts
   * - **Escalation Workflows**
     - Configurable escalation rules with severity bumping and notifications
   * - **Silencing Rules**
     - Suppress notifications during maintenance windows
   * - **Inhibition Rules**
     - Suppress related alerts during cascading failures
   * - **Approval Workflows**
     - Configurable approval rules for automated actions

Architecture Overview
---------------------

.. mermaid::

    flowchart TB
        subgraph Clients
            API[REST API Clients]
            MCP[MCP Servers]
            UI[Web Dashboard]
        end

        subgraph "Shizuha Hub"
            REST[REST API]
            VS[ViewSets]
            SVC[Services]
            MDL[Models]
        end

        subgraph Services
            DEDUP[Deduplication]
            ESC[Escalation]
            SIL[Silencing]
            INH[Inhibition]
        end

        subgraph Storage
            PG[(PostgreSQL)]
            RD[(Redis)]
        end

        API --> REST
        MCP --> REST
        UI --> REST
        REST --> VS
        VS --> SVC
        SVC --> MDL
        MDL --> PG
        SVC --> DEDUP
        SVC --> ESC
        SVC --> SIL
        SVC --> INH
        ESC --> RD

Item Modes
----------

Shizuha Hub uses a unified ``Item`` model with three modes:

.. tabs::

    .. tab:: Tasks

        Personal task management including:

        - Reminders and to-dos
        - Calendar events
        - Recurring tasks
        - Deadline tracking

    .. tab:: Actions

        System-generated items requiring approval:

        - Automated workflows
        - Callback execution
        - Approval chains
        - Retry handling

    .. tab:: Alerts

        System alerts with:

        - Fingerprint deduplication
        - Severity levels
        - Acknowledgment workflow
        - Escalation support

.. toctree::
   :maxdepth: 2
   :caption: User Guide
   :hidden:

   guides/quickstart
   guides/installation
   guides/configuration
   guides/docker
   guides/organizations

.. toctree::
   :maxdepth: 2
   :caption: API Reference
   :hidden:

   api/index
   api/items
   api/alerts
   api/actions
   api/organizations
   api/projects
   api/rules
   api/openapi

.. toctree::
   :maxdepth: 2
   :caption: Models
   :hidden:

   models/index
   models/item
   models/rules
   models/supporting
   models/organizations

.. toctree::
   :maxdepth: 2
   :caption: Services
   :hidden:

   services/index
   services/deduplication
   services/escalation
   services/silencing
   services/inhibition
   services/permissions

.. toctree::
   :maxdepth: 2
   :caption: Development
   :hidden:

   development/contributing
   development/testing
   development/changelog

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
