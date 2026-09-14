(function_declaration name: (identifier) @def.name parameters: (formal_parameters) @def.params) @def.node
(class_declaration name: (type_identifier) @def.name) @def.node
(method_definition name: (property_identifier) @def.name parameters: (formal_parameters) @def.params) @def.node
(lexical_declaration (variable_declarator name: (identifier) @def.name value: (arrow_function parameters: (formal_parameters) @def.params))) @def.node
(import_statement source: (string) @import.module)
(call_expression function: (identifier) @call.callee) @call.node
(call_expression function: (member_expression property: (property_identifier) @call.callee)) @call.node
