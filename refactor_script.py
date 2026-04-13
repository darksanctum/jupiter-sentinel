import libcst as cst
import glob
import os

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
            
        # Find where to insert imports (after docstring and __future__ imports)
        insert_idx = 0
        for i, stmt in enumerate(body):
            if i == 0 and isinstance(stmt, cst.SimpleStatementLine) and isinstance(stmt.body[0], cst.Expr) and isinstance(stmt.body[0].value, cst.SimpleString):
                insert_idx = i + 1
            elif isinstance(stmt, cst.SimpleStatementLine) and isinstance(stmt.body[0], cst.ImportFrom) and stmt.body[0].module and stmt.body[0].module.value == "__future__":
                insert_idx = i + 1
            elif isinstance(stmt, cst.SimpleStatementLine) and isinstance(stmt.body[0], cst.Expr) and isinstance(stmt.body[0].value, cst.SimpleString):
                pass # multiple docstrings?
            else:
                break
        
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
            
            # Change print(a, b) to logging.debug("%s %s", a, b)
            # Exclude kwargs like sep=, end=, file= from logging.debug
            valid_args = [arg for arg in updated_node.args if arg.keyword is None]
            
            if len(valid_args) > 0:
                fmt_str = " ".join(["%s"] * len(valid_args))
                new_args = [cst.Arg(value=cst.SimpleString(f'"{fmt_str}"'))] + valid_args
            else:
                new_args = [cst.Arg(value=cst.SimpleString('""'))]
                
            return updated_node.with_changes(func=new_func, args=new_args)
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
            
        # Add type hints to parameters
        params = []
        for param in updated_node.params.params:
            if param.annotation is None and param.name.value not in ["self", "cls"]:
                new_param = param.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
                params.append(new_param)
            else:
                params.append(param)
                
        # kwonlyparams
        kwonlyparams = []
        for param in updated_node.params.kwonly_params:
            if param.annotation is None and param.name.value not in ["self", "cls"]:
                new_param = param.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
                kwonlyparams.append(new_param)
            else:
                kwonlyparams.append(param)
                
        # posonlyparams
        posonlyparams = []
        for param in updated_node.params.posonly_params:
            if param.annotation is None and param.name.value not in ["self", "cls"]:
                new_param = param.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
                posonlyparams.append(new_param)
            else:
                posonlyparams.append(param)
                
        # star_arg
        star_arg = updated_node.params.star_arg
        if isinstance(star_arg, cst.Param) and star_arg.annotation is None:
            star_arg = star_arg.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
            
        # star_kwarg
        star_kwarg = updated_node.params.star_kwarg
        if isinstance(star_kwarg, cst.Param) and star_kwarg.annotation is None:
            star_kwarg = star_kwarg.with_changes(annotation=cst.Annotation(annotation=cst.Name("Any")))
                
        new_params_node = updated_node.params.with_changes(
            params=params, 
            kwonly_params=kwonlyparams,
            posonly_params=posonlyparams,
            star_arg=star_arg,
            star_kwarg=star_kwarg
        )
        
        returns = updated_node.returns
        if returns is None:
            if updated_node.name.value == "__init__":
                returns = cst.Annotation(annotation=cst.Name("None"))
            else:
                returns = cst.Annotation(annotation=cst.Name("Any"))
            
        return updated_node.with_changes(
            body=updated_node.body.with_changes(body=body),
            params=new_params_node,
            returns=returns
        )

def process_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    
    try:
        tree = cst.parse_module(code)
    except Exception as e:
        print(f"Failed to parse {filepath}: {e}")
        return

    transformer = RefactorTransformer()
    modified_tree = tree.visit(transformer)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(modified_tree.code)

if __name__ == "__main__":
    files = glob.glob("src/**/*.py", recursive=True)
    for f in files:
        process_file(f)
