"""Consumers retain one shared gateway path. Transport/secret tests: test_openrouter."""
import inspect
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import bootstrap,expand,generate
class SharedGatewayTests(unittest.TestCase):
    def test_consumers_share_the_chain(self):
        for fn in (bootstrap._ask_json,expand._convert_llm,generate.draft):
            self.assertIn('pick_llm',inspect.getsource(fn))
