import libcst as cst

class RefactorTransformer(cst.CSTTransformer):
    def __init__(self):
        super().__init__()
        self.has_logging_import = False
        self.has_typing_any_import = False

    def visit_Import(self, node):
        for name in node.names:
            if name.name.value == "logging":
                self.has_logging_import = True
        return False

    def visit_ImportFrom(self, node):
        if node.module and node.module.value == "logging":
            self.has_logging_import = True
        if node.module and node.module.value == "typing":
            for name in node.names:
                if isinstance(name, cst.ImportAlias) and name.name.value == "Any":
                    self.has_typing_any_import = True
        return False

    def leave_Module(self, original_node, updated_node):
        body = list(updated_node.body)
        
        has_module_docstring = False
        if len(body) > 0 and isinstance(body[0], cst.SimpleStatementLine):
            if isinstance(body[0].body[0], cst.Expr) and isinstance(body[0].body[0].value, cst.SimpleString):
                has_module_docstring = True
                
        if not has_module_docstring:
            docstring = cst.SimpleStatementLine(body=[cst.Expr(value=cst.SimpleString('"""Module explaining what this file does."""'))])
            body.insert(0, docstring)
            
        insert_idx = 1 if not has_module_docstring else 1 # Insert after docstring
        
        if not self.has_logging_import:
            import_stmt = cst.SimpleStatementLine(body=[cst.Import(names=[cst.ImportAlias(name=cst.Name("logging"))])])
            body.insert(insert_idx, import_stmt)
            insert_idx += 1
            
        if not self.has_typing_any_import:
            import_any_stmt = cst.SimpleStatementLine(body=[cst.ImportFrom(module=cst.Name("typing"), names=[cst.ImportAlias(name=cst.Name("Any"))])])
            body.insert(insert_idx, import_any_stmt)
            
        return updated_node.with_changes(body=body)

    def leave_Comment(self, original_node, updated_node):
        if "todo" in updated_node.value.lower():
            return cst.RemoveFromParent()
        return updated_node

    def leave_Call(self, original_node, updated_node):
        if isinstance(updated_node.func, cst.Name) and updated_node.func.value == "print":
            new_func = cst.Attribute(
                value=cst.Name("logging"),
                attr=cst.Name("debug")
            )
            return updated_node.with_changes(func=new_func)
        return updated_node

    def leave_FunctionDef(self, original_node, updated_node):
        body = list(updated_node.body.body)
        has_docstring = False
        if len(body) > 0 and isinstance(body[0], cst.SimpleStatementLine):
            if isinstance(body[0].body[0], cst.Expr) and isinstance(body[0].body[0].value, cst.SimpleString):
                has_docstring = True
                
        if not has_docstring:
            docstring = cst.SimpleStatementLine(body=[cst.Expr(value=cst.SimpleString('"""Function docstring."""'))])
            body.insert(0, docstring)
            
        params = []
        for param in updated_node.params.params:
            if param.annotation is None and param.name.value not in ["self", "cls"]:
                new_param = param.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
                params.append(new_param)
            else:
                params.append(param)
                
        new_params_node = updated_node.params.with_changes(params=params)
        
        returns = updated_node.returns
        if returns is None and updated_node.name.value != "__init__":
            returns = cst.Annotation(annotation=cst.Name("Any"))
            
        return updated_node.with_changes(
            body=updated_node.body.with_changes(body=body),
            params=new_params_node,
            returns=returns
        )

code = """
# TODO: Fix this
def hello(name):
    print("Hello", name)
"""

tree = cst.parse_module(code)
transformer = RefactorTransformer()
modified_tree = tree.visit(transformer)
print(modified_tree.code)
