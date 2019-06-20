import sys;

from graph import Graph;
from validate.utilities import report;

def test(graph, actions, stream = sys.stderr):
  for node in graph.nodes:
    if not isinstance(node.label, str) or len(node.label) == 0:
      report(graph,
             "invalid label ‘{}’".format(node.label),
             node = node, stream = stream);
