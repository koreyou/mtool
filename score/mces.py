import sys
from operator import itemgetter

import numpy as np

from score.core import explode, fscore, intersect
from score.ucca import identify

counter = 0


def reindex(i):
    return -2 - i


def get_or_update(index, key):
    return index.setdefault(key, len(index))


class InternalGraph():

    def __init__(self, graph, index):
        self.node2id = dict()
        self.nodes = []
        self.edges = []
        for i, node in enumerate(graph.nodes):
            self.node2id[node] = i
            self.nodes.append(i)
        for i, edge in enumerate(graph.edges):
            src = graph.find_node(edge.src)
            src = self.node2id[src]
            tgt = graph.find_node(edge.tgt)
            tgt = self.node2id[tgt]
            self.edges.append((src, tgt, edge.lab))
            if edge.attributes:
                for prop, val in zip(edge.attributes, edge.values):
                    self.edges.append((src, tgt, ("E", prop, val)))
        #
        # Build the pseudo-edges. These have target nodes that are
        # unique for the value of the label, anchor, property.
        #
        if index is None:
            index = dict()
        for i, node in enumerate(graph.nodes):
            # labels
            j = get_or_update(index, ("L", node.label))
            self.edges.append((i, reindex(j), None))
            # tops
            if node.is_top:
                j = get_or_update(index, ("T"))
                self.edges.append((i, reindex(j), None))
            # anchors
            if node.anchors is not None:
                for anchor in node.anchors:
                    j = get_or_update(index, ("A", anchor["from"],
                                              anchor["to"]))
                    self.edges.append((i, reindex(j), None))
            # properties
            if node.properties:
                for prop, val in zip(node.properties, node.values):
                    j = get_or_update(index, ("P", prop, val))
                    self.edges.append((i, reindex(j), None))


def initial_node_correspondences(graph1, graph2, identities1=None, identities2=None):
    #
    # in the following, we assume that nodes in raw and internal
    # graphs correspond by position into the .nodes. list
    #
    shape = (len(graph1.nodes), len(graph2.nodes) + 1)
    rewards = np.zeros(shape, dtype=np.int);
    edges = np.zeros(shape, dtype=np.int);
    anchors = np.zeros(shape, dtype=np.int);

    queue = [];
    for i, node1 in enumerate(graph1.nodes):
        for j, node2 in enumerate(graph2.nodes + [None]):
            rewards[i, j], _, _, _ = node1.compare(node2);
            if node2 is not None:
                #
                # also determine the maximum number of edge matches we
                # can hope to score, for each node-node correspondence
                #
                for edge1 in graph1.edges:
                    for edge2 in graph2.edges:
                        if edge1.lab == edge2.lab and \
                           (edge1.src == node1.id and edge2.src == node2.id or
                            edge1.tgt == node1.id and edge2.tgt == node2.id):
                            edges[i, j] += 1;
                # and the overlap of UCCA yields
                if identities1 and identities2:
                    anchors[i, j] += len(identities1[node1.id] &
                                         identities2[node2.id])
            queue.append((rewards[i, j], edges[i, j], anchors[i, j],
                          i, j if node2 is not None else None));
    pairs = [];
    sources = set();
    targets = set();
    for _, _, _, i, j in sorted(queue, key = itemgetter(0, 1, 2), reverse = True):
        if i not in sources and j not in targets:
            pairs.append((i, j));
            sources.add(i);
            if j is not None: targets.add(j);
    #
    # adjust rewards to use edge potential as a secondary key; maybe
    # we should rather pass around edges and adjust sorted_splits()?
    # for even better initialization, consider edge attributes too?
    #
    rewards *= 10
    rewards += edges + anchors
    return pairs, rewards;


# The next function constructs the initial table with the candidates
# for the edge-to-edge correspondence. Each edge in the source graph
# is mapped to the set of all edges in the target graph.

def make_edge_candidates(graph1, graph2):
    candidates = dict()
    for raw_edge1 in graph1.edges:
        src1, tgt1, lab1 = raw_edge1
        edge1 = src1, tgt1
        candidates[edge1] = edge1_candidates = set()
        for raw_edge2 in graph2.edges:
            src2, tgt2, lab2 = raw_edge2
            edge2 = src2, tgt2
            if tgt1 < 0:
                # Edge edge1 is a pseudoedge. This can only map to
                # another pseudoedge pointing to the same pseudonode.
                if tgt2 == tgt1 and lab1 == lab2:
                    edge1_candidates.add(edge2)
            elif tgt2 >= 0 and lab1 == lab2:
                # Edge edge1 is a real edge. This can only map to
                # another real edge.
                edge1_candidates.add(edge2)
    return candidates


# The next function updates the table with the candidates for the
# edge-to-edge correspondence when node `i` is tentatively mapped to
# node `j`.

def update_edge_candidates(edge_candidates, i, j):
    new_candidates = dict()
    new_potential = 0
    for edge1, edge1_candidates in edge_candidates.items():
        if i in edge1:
            # Edge edge1 is affected by the tentative assignment. Need
            # to explicitly construct the new set of candidates for
            # edge1.
            # Both edges share the same source/target node
            # (modulo the tentative assignment).
            src1, tgt1 = edge1
            edge1_candidates = {(src2, tgt2) for src2, tgt2 in edge1_candidates
                                if src1 == i and src2 == j or tgt1 == i and tgt2 == j}
        else:
            # Edge edge1 is not affected by the tentative
            # assignment. Just include a pointer to the candidates for
            # edge1 in the old assignment.
            edge1_candidates = edge1_candidates
        new_candidates[edge1] = edge1_candidates
        new_potential += 1 if edge1_candidates else 0
    return new_candidates, new_potential


def splits(xs):
    # The source graph node is mapped to some target graph node (x).
    for i, x in enumerate(xs):
        yield x, xs[:i] + xs[i+1:]
    # The source graph node is not mapped to any target graph node.
    yield -1, xs


def sorted_splits(i, xs, rewards):
    sorted_xs = sorted(xs, key=rewards[i].item, reverse=True)
    yield from splits(sorted_xs)


# UCCA-specific rule:
# Do not pursue correspondences of nodes i and j in case there is
# a node dominated by i whose correspondence is not dominated by j

def domination_conflict(cv, i, j, dominated1, dominated2):
    if not dominated1 or not dominated2 or i < 0 or j < 0:
        return False
    dominated_i = dominated1[i]
    dominated_j = dominated2[j]
    for _i, _j in cv.items():
        if _i >= 0 and _j >= 0 and \
                _i in dominated_i and \
                _j not in dominated_j:
            return True
    return False

# Find all maximum edge correspondences between the source graph
# (graph1) and the target graph (graph2). This implements the
# algorithm of McGregor (1982).

def correspondences(graph1, graph2, pairs, rewards, limit=0, trace=0,
                    dominated1=None, dominated2=None):
    global counter
    bilexical = graph1.flavor == 0 and graph2.flavor == 0
    index = dict()
    graph1 = InternalGraph(graph1, index)
    graph2 = InternalGraph(graph2, index)
    cv = dict()
    ce = make_edge_candidates(graph1, graph2)
    # Visit the source graph nodes in descending order of rewards.
    source_todo = [pair[0] for pair in pairs]
    todo = [(cv, ce, source_todo, sorted_splits(
        source_todo[0], graph2.nodes, rewards))]
    n_matched = 0
    while todo and (limit == 0 or counter <= limit):
        cv, ce, source_todo, untried = todo[-1]
        i = source_todo[0]
        try:
            j, new_untried = next(untried)
            if cv:
                if bilexical:  # respect node ordering in bi-lexical graphs
                    max_j = max((_j for _i, _j in cv.items() if _i < i), default=-1)
                    if 0 <= j < max_j + 1:
                        continue
                elif domination_conflict(cv, i, j, dominated1, dominated2):
                    continue
            counter += 1
            if trace > 2: print("({}:{}) ".format(i, j), end="")
            new_cv = dict(cv)
            new_cv[i] = j
            new_ce, new_potential = update_edge_candidates(ce, i, j)
            if new_potential > n_matched:
                new_source_todo = source_todo[1:]
                if new_source_todo:
                    if trace > 2: print("> ", end="")
                    todo.append((new_cv, new_ce, new_source_todo,
                                 sorted_splits(new_source_todo[0],
                                               new_untried, rewards)))
                else:
                    if trace > 2: print()
                    yield new_cv, new_ce
                    n_matched = new_potential
        except StopIteration:
            if trace > 2: print("< ")
            todo.pop()


def is_valid(correspondence):
    return all(len(x) <= 1 for x in correspondence.values())


def is_injective(correspondence):
    seen = set()
    for xs in correspondence.values():
        for x in xs:
            if x in seen:
                return False
            else:
                seen.add(x)
    return True


def evaluate(gold, system, format="json", limit=500000, trace=0):

    global counter;

    def update(total, counts):
        for key in ("g", "s", "c"):
            total[key] += counts[key];

    def finalize(counts):
        p, r, f = fscore(counts["g"], counts["s"], counts["c"]);
        counts.update({"p": p, "r": r, "f": f});

    if not limit: limit = 500000;
        
    total_matches = total_steps = 0;
    total_pairs = 0;
    total_tops = {"g": 0, "s": 0, "c": 0}
    total_labels = {"g": 0, "s": 0, "c": 0}
    total_properties = {"g": 0, "s": 0, "c": 0}
    total_anchors = {"g": 0, "s": 0, "c": 0}
    total_edges = {"g": 0, "s": 0, "c": 0}
    total_attributes = {"g": 0, "s": 0, "c": 0}
    scores = dict() if trace else None
    for g, s in intersect(gold, system):
        counter = 0

        g_identities, s_identities, g_dominated, s_dominated = \
            identities(g, s)
        pairs, rewards = initial_node_correspondences(
            g, s, identities1=g_identities, identities2=s_identities)
        if trace > 1:
            print("\n\ngraph #{}".format(g.id))
            print("Number of gold nodes: {}".format(len(g.nodes)))
            print("Number of system nodes: {}".format(len(s.nodes)))
            print("Number of edges: {}".format(len(g.edges)))
            if trace > 2:
                print("Rewards and Pairs:\n{}\n{}\n".format(rewards, pairs))
        n_matched = 0
        best_cv, best_ce = None, None
        for i, (cv, ce) in enumerate(correspondences(
                g, s, pairs, rewards, limit, trace,
                dominated1=g_dominated, dominated2=s_dominated)):
            assert is_valid(ce)
            assert is_injective(ce)
            n = sum(map(len, ce.values()))
            if n > n_matched:
                if trace > 1:
                    print("\n[{}] solution #{}; matches: {}"
                          "".format(counter, i, n));
                n_matched = n
                best_cv, best_ce = cv, ce
        total_matches += n_matched;
        total_steps += counter;
        tops, labels, properties, anchors, edges, attributes \
            = g.score(s, best_cv);
        if trace:
            if g.id in scores:
                print("mces.evaluate(): duplicate graph identifier: {}"
                      "".format(g.id), file = sys.stderr);
            scores[g.id] = {"tops": tops, "labels": labels,
                            "properties": properties, "anchors": anchors,
                            "edges": edges, "attributes": attributes};
        update(total_tops, tops);
        update(total_labels, labels);
        update(total_properties, properties);
        update(total_anchors, anchors);
        update(total_edges, edges);
        update(total_attributes, attributes);
        total_pairs += 1;
        if trace > 1:
            print("[{}] Number of edges in correspondence: {}"
                  "".format(counter, n_matched))
            print("[{}] Total matches: {}".format(total_steps, total_matches));
            print("tops: {}\nlabels: {}\nproperties: {}\nanchors: {}"
                  "\nedges: {}\nattributes:{}"
                  "".format(tops, labels, properties, anchors,
                            edges, attributes));
            if trace > 2:
                print(best_cv)
                print(best_ce)

    total_all = {"g": 0, "s": 0, "c": 0};
    for counts in [total_tops, total_labels, total_properties, total_anchors,
                   total_edges, total_attributes]:
        update(total_all, counts);
        finalize(counts);
    finalize(total_all);
    result = {"n": total_pairs,
              "tops": total_tops, "labels": total_labels,
              "properties": total_properties, "anchors": total_anchors,
              "edges": total_edges, "attributes": total_attributes,
              "all": total_all};
    if trace: result["scores"] = scores;
    return result;


def identities(g, s):
    #
    # use overlap of UCCA yields in picking initial node pairing
    #
    if g.framework == "ucca" and g.input \
            and s.framework == "ucca" and s.input:
        g_identities = dict()
        s_identities = dict()
        g_dominated = dict()
        s_dominated = dict()
        for node in g.nodes:
            g_identities, g_dominated = \
                identify(g, node.id, g_identities, g_dominated)
        g_identities = {key: explode(g.input, value)
                        for key, value in g_identities.items()}
        for node in s.nodes:
            s_identities, s_dominated = \
                identify(s, node.id, s_identities, s_dominated)
        s_identities = {key: explode(s.input, value)
                        for key, value in s_identities.items()}
    else:
        g_identities = s_identities = g_dominated = s_dominated = None
    return g_identities, s_identities, g_dominated, s_dominated
