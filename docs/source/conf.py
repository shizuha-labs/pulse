"""
Sphinx Configuration for Shizuha Hub Documentation
===================================================

Auto-generated documentation with live reload support.
"""

import os
import sys
from datetime import datetime

# -- Path setup --------------------------------------------------------------
# Add the project root to sys.path for autodoc
sys.path.insert(0, os.path.abspath('../..'))

# Setup Django before importing any Django modules
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taskmanager.settings')

# Minimal Django setup for documentation
import django
django.setup()

# -- Project information -----------------------------------------------------
project = 'Shizuha Pulse'
copyright = f'{datetime.now().year}, Shizuha Stack'
author = 'Shizuha Stack Team'
release = '1.0.0'
version = '1.0'

# -- General configuration ---------------------------------------------------
extensions = [
    # Core Sphinx extensions
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon',
    'sphinx.ext.intersphinx',
    'sphinx.ext.todo',
    'sphinx.ext.coverage',
    'sphinx.ext.graphviz',

    # Third-party extensions
    'sphinx_autodoc_typehints',
    'sphinx_copybutton',
    'sphinx_design',
    'sphinxcontrib.mermaid',
    'sphinx_tabs.tabs',
    'myst_parser',
    'sphinxcontrib.openapi',
]

# Templates path
templates_path = ['_templates']

# Patterns to exclude
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# The suffix of source filenames
source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}

# The master toctree document
master_doc = 'index'

# -- Options for HTML output -------------------------------------------------
html_theme = 'furo'

html_theme_options = {
    "light_css_variables": {
        "color-brand-primary": "#6366f1",
        "color-brand-content": "#6366f1",
        "color-admonition-background": "rgba(99, 102, 241, 0.1)",
    },
    "dark_css_variables": {
        "color-brand-primary": "#818cf8",
        "color-brand-content": "#818cf8",
        "color-admonition-background": "rgba(129, 140, 248, 0.1)",
    },
    "sidebar_hide_name": False,
    "navigation_with_keys": True,
    "top_of_page_button": "edit",
    "source_repository": "https://github.com/SoniCoder/shizuha-tasks",
    "source_branch": "main",
    "source_directory": "docs/source/",
}

html_static_path = ['_static']
html_css_files = ['custom.css']
html_title = "Shizuha Pulse Documentation"
html_short_title = "Shizuha Pulse"
html_logo = None  # Add logo path if available
html_favicon = None  # Add favicon path if available

# -- Autodoc configuration ---------------------------------------------------
autodoc_default_options = {
    'members': True,
    'member-order': 'bysource',
    'special-members': '__init__',
    'undoc-members': True,
    'exclude-members': '__weakref__',
    'show-inheritance': True,
    'inherited-members': False,
}

autodoc_member_order = 'bysource'
autodoc_typehints = 'both'
autodoc_typehints_format = 'short'
autodoc_class_signature = 'separated'

# Autosummary settings
autosummary_generate = True
autosummary_generate_overwrite = True

# -- Napoleon settings (Google/NumPy docstrings) ----------------------------
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_use_keyword = True
napoleon_attr_annotations = True

# -- Type hints configuration ------------------------------------------------
typehints_defaults = 'braces'
always_document_param_types = True
typehints_document_rtype = True

# -- Intersphinx configuration -----------------------------------------------
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'django': ('https://docs.djangoproject.com/en/4.2/', 'https://docs.djangoproject.com/en/4.2/_objects/'),
    'celery': ('https://docs.celeryq.dev/en/stable/', None),
}

# -- Mermaid configuration ---------------------------------------------------
mermaid_version = "10.6.1"
mermaid_init_js = """
mermaid.initialize({
    startOnLoad: true,
    theme: 'base',
    themeVariables: {
        primaryColor: '#6366f1',
        primaryTextColor: '#fff',
        primaryBorderColor: '#4f46e5',
        lineColor: '#94a3b8',
        secondaryColor: '#e0e7ff',
        tertiaryColor: '#f1f5f9'
    }
});
"""

# -- MyST (Markdown) configuration -------------------------------------------
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "tasklist",
    "attrs_block",
    "attrs_inline",
]
myst_heading_anchors = 3

# -- Todo extension configuration --------------------------------------------
todo_include_todos = True

# -- Copybutton configuration ------------------------------------------------
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: "
copybutton_prompt_is_regexp = True

# -- OpenAPI configuration ---------------------------------------------------
# This will be used to embed generated OpenAPI specs
openapi_default_renderer = 'httpdomain'

# -- Coverage configuration --------------------------------------------------
coverage_show_missing_items = True

# -- Highlight configuration --------------------------------------------------
# Default language for code blocks without explicit language
highlight_language = 'none'

# Suppress specific warnings
suppress_warnings = [
    'misc.highlighting_failure',
]
