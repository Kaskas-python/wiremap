(function_declaration name: (identifier) @def.name parameters: (parameter_list) @def.params) @def.node
(method_declaration name: (field_identifier) @def.name parameters: (parameter_list) @def.params) @def.node
(type_spec name: (type_identifier) @def.name) @def.node
(import_spec path: (interpreted_string_literal) @import.module)
(call_expression function: (identifier) @call.callee) @call.node
(call_expression function: (selector_expression field: (field_identifier) @call.callee)) @call.node
