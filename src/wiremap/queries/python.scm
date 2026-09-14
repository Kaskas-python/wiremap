(function_definition name: (identifier) @def.name parameters: (parameters) @def.params) @def.node
(class_definition name: (identifier) @def.name) @def.node
(decorated_definition (decorator) @deco.node definition: (_) @deco.target)
(import_statement name: (dotted_name) @import.module)
(import_from_statement module_name: (dotted_name) @import.module name: (dotted_name) @import.name)
(call function: (identifier) @call.callee) @call.node
(call function: (attribute attribute: (identifier) @call.callee) @call.attr) @call.node
