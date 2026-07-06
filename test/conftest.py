"""
Pytest configuration and fixtures.
Ensures the project root is on sys.path for imports to work correctly.
"""
import sys
import os

# Add project root to Python path so 'src' and 'test' modules can be imported
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Add test directory to Python path so test modules like InkscapeHarness can be imported
test_dir = os.path.dirname(os.path.abspath(__file__))
if test_dir not in sys.path:
    sys.path.insert(0, test_dir)
