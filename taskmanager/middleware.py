"""
Custom middleware for handling reverse proxy configurations.
"""

class ScriptNameMiddleware:
    """
    Middleware to handle the X-Script-Name header from the reverse proxy (Nginx).
    This dynamically sets the URL prefix for each request, which is more reliable
    than the global FORCE_SCRIPT_NAME setting, especially for complex redirect
    scenarios like admin login.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check for the X-Script-Name header sent by Nginx
        script_name = request.META.get('HTTP_X_SCRIPT_NAME')
        if script_name:
            # Set the script_name in multiple places to ensure Django uses it
            request.META['SCRIPT_NAME'] = script_name
            # Also set path_info to be relative to the script name
            if request.path_info.startswith(script_name):
                request.path_info = request.path_info[len(script_name):]
            
        response = self.get_response(request)
        return response
