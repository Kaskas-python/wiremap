(function_item name: (identifier) @def.name parameters: (parameters) @def.params) @def.node
(struct_item name: (type_identifier) @def.name) @def.node
(enum_item name: (type_identifier) @def.name) @def.node
(trait_item name: (type_identifier) @def.name) @def.node
(use_declaration argument: (_) @import.module)
(call_expression function: (identifier) @call.callee) @call.node
(call_expression function: (field_expression field: (field_identifier) @call.callee)) @call.node
(call_expression function: (scoped_identifier name: (identifier) @call.callee)) @call.node
