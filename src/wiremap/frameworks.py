from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    framework: str
    language: str
    query: str
    edge_kind: str
    target: str


RULES: list[Rule] = [
    Rule(
        "fastapi",
        "python",
        "(call function: (identifier) @fn "
        "arguments: (argument_list (identifier) @target) "
        '(#eq? @fn "Depends"))',
        "depends",
        "target",
    ),
    Rule(
        "fastapi",
        "python",
        "(decorated_definition (decorator (call function: "
        "(attribute attribute: (identifier) @m))) definition: (_) @self "
        '(#match? @m "^(get|post|put|patch|delete|websocket)$"))',
        "route",
        "self",
    ),
    Rule(
        "langgraph",
        "python",
        "(call function: (attribute attribute: (identifier) @m) "
        "arguments: (argument_list (string) @label . (identifier) @target) "
        '(#eq? @m "add_node"))',
        "graph_node",
        "target",
    ),
    Rule(
        "langgraph",
        "python",
        "(call function: (attribute attribute: (identifier) @m) "
        "arguments: (argument_list (string) @a . (string) @b) "
        '(#eq? @m "add_edge"))',
        "graph_edge",
        "label:a->b",
    ),
    Rule(
        "langgraph",
        "python",
        "(call function: (attribute attribute: (identifier) @m) "
        "arguments: (argument_list (string) @a . (identifier) @target) "
        '(#eq? @m "add_conditional_edges"))',
        "graph_edge",
        "label:a->target",
    ),
    Rule(
        "sqlalchemy",
        "python",
        "(call function: (identifier) @fn "
        "arguments: (argument_list (string) @target) "
        '(#eq? @fn "relationship"))',
        "relationship",
        "target",
    ),
    Rule(
        "celery",
        "python",
        "(decorated_definition (decorator [(identifier) @d "
        "(attribute attribute: (identifier) @d) (call function: (identifier) @d) "
        "(call function: (attribute attribute: (identifier) @d))]) "
        'definition: (_) @self (#match? @d "^(shared_task|task)$"))',
        "task",
        "self",
    ),
    Rule(
        "celery",
        "python",
        "(call function: (attribute object: (identifier) @target "
        "attribute: (identifier) @m) "
        '(#match? @m "^(delay|apply_async)$"))',
        "calls",
        "target",
    ),
]
