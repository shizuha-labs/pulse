#!/bin/bash
# Push changes to upstream
set -e

BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "Pushing $BRANCH to origin..."
git push origin "$BRANCH"
echo "Done!"
