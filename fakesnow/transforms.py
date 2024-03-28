from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Literal, cast

import sqlglot
from sqlglot import exp

from fakesnow.global_database import USERS_TABLE_FQ_NAME

MISSING_DATABASE = "missing_database"
SUCCESS_NOP = sqlglot.parse_one("SELECT 'Statement executed successfully.'")


def array_size(expression: exp.Expression) -> exp.Expression:
    if isinstance(expression, exp.ArraySize):
        # case is used to convert 0 to null, because null is returned by duckdb when no case matches
        jal = exp.Anonymous(this="json_array_length", expressions=[expression.this])
        return exp.Case(ifs=[exp.If(this=jal, true=jal)])

    return expression


# TODO: move this into a Dialect as a transpilation
def create_database(expression: exp.Expression, db_path: Path | None = None) -> exp.Expression:
    """Transform create database to attach database.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("CREATE database foo").transform(create_database).sql()
        'ATTACH DATABASE ':memory:' as foo'
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression, with the database name stored in the create_db_name arg.
    """

    if isinstance(expression, exp.Create) and str(expression.args.get("kind")).upper() == "DATABASE":
        ident = expression.find(exp.Identifier)
        assert ident, f"No identifier in {expression.sql}"
        db_name = ident.this
        db_file = f"{db_path/db_name}.db" if db_path else ":memory:"

        return exp.Command(
            this="ATTACH",
            expression=exp.Literal(this=f"DATABASE '{db_file}' AS {db_name}", is_string=True),
            create_db_name=db_name,
        )

    return expression


SQL_DESCRIBE_TABLE = Template(
    """
SELECT
    column_name AS "name",
    CASE WHEN data_type = 'NUMBER' THEN 'NUMBER(' || numeric_precision || ',' || numeric_scale || ')'
         WHEN data_type = 'TEXT' THEN 'VARCHAR(' || coalesce(character_maximum_length,16777216)  || ')'
         WHEN data_type = 'TIMESTAMP_NTZ' THEN 'TIMESTAMP_NTZ(9)'
         WHEN data_type = 'TIMESTAMP_TZ' THEN 'TIMESTAMP_TZ(9)'
         WHEN data_type = 'TIME' THEN 'TIME(9)'
         WHEN data_type = 'BINARY' THEN 'BINARY(8388608)'
        ELSE data_type END AS "type",
    'COLUMN' AS "kind",
    CASE WHEN is_nullable = 'YES' THEN 'Y' ELSE 'N' END AS "null?",
    column_default AS "default",
    'N' AS "primary key",
    'N' AS "unique key",
    NULL AS "check",
    NULL AS "expression",
    NULL AS "comment",
    NULL AS "policy name",
    NULL AS "privacy domain",
FROM information_schema._fs_columns_snowflake
WHERE table_catalog = '${catalog}' AND table_schema = '${schema}' AND table_name = '${table}'
ORDER BY ordinal_position
"""
)


def describe_table(
    expression: exp.Expression, current_database: str | None = None, current_schema: str | None = None
) -> exp.Expression:
    """Redirect to the information_schema._fs_describe_table to match snowflake.

    See https://docs.snowflake.com/en/sql-reference/sql/desc-table
    """

    if (
        isinstance(expression, exp.Describe)
        and (kind := expression.args.get("kind"))
        and isinstance(kind, str)
        and kind.upper() == "TABLE"
        and (table := expression.find(exp.Table))
    ):
        catalog = table.catalog or current_database
        schema = table.db or current_schema

        return sqlglot.parse_one(
            SQL_DESCRIBE_TABLE.substitute(catalog=catalog, schema=schema, table=table.name),
            read="duckdb",
        )

    return expression


def drop_schema_cascade(expression: exp.Expression) -> exp.Expression:
    """Drop schema cascade.

    By default duckdb won't delete a schema if it contains tables, whereas snowflake will.
    So we add the cascade keyword to mimic snowflake's behaviour.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("DROP SCHEMA schema1").transform(remove_comment).sql()
        'DROP SCHEMA schema1 cascade'
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    if (
        not isinstance(expression, exp.Drop)
        or not (kind := expression.args.get("kind"))
        or not isinstance(kind, str)
        or kind.upper() != "SCHEMA"
    ):
        return expression

    new = expression.copy()
    new.args["cascade"] = True
    return new


def extract_comment_on_columns(expression: exp.Expression) -> exp.Expression:
    """Extract column comments, removing it from the Expression.

    duckdb doesn't support comments. So we remove them from the expression and store them in the column_comment arg.
    We also replace the transform the expression to NOP if the statement can't be executed by duckdb.

    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression, with any comment stored in the new 'table_comment' arg.
    """

    if isinstance(expression, exp.AlterTable) and (actions := expression.args.get("actions")):
        new_actions: list[exp.Expression] = []
        col_comments: list[tuple[str, str]] = []
        for a in actions:
            if isinstance(a, exp.AlterColumn) and (comment := a.args.get("comment")):
                col_comments.append((a.name, comment.this))
            else:
                new_actions.append(a)
        if not new_actions:
            expression = SUCCESS_NOP.copy()
        else:
            expression.set("actions", new_actions)
        expression.args["col_comments"] = col_comments

    return expression


def extract_comment_on_table(expression: exp.Expression) -> exp.Expression:
    """Extract table comment, removing it from the Expression.

    duckdb doesn't support comments. So we remove them from the expression and store them in the table_comment arg.
    We also replace the transform the expression to NOP if the statement can't be executed by duckdb.

    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression, with any comment stored in the new 'table_comment' arg.
    """

    if isinstance(expression, exp.Create) and (table := expression.find(exp.Table)):
        comment = None
        if props := cast(exp.Properties, expression.args.get("properties")):
            other_props = []
            for p in props.expressions:
                if isinstance(p, exp.SchemaCommentProperty) and (isinstance(p.this, (exp.Literal, exp.Identifier))):
                    comment = p.this.this
                else:
                    other_props.append(p)

            new = expression.copy()
            new_props: exp.Properties = new.args["properties"]
            new_props.set("expressions", other_props)
            new.args["table_comment"] = (table, comment)
            return new
    elif (
        isinstance(expression, exp.Comment)
        and (cexp := expression.args.get("expression"))
        and (table := expression.find(exp.Table))
    ):
        new = SUCCESS_NOP.copy()
        new.args["table_comment"] = (table, cexp.this)
        return new
    elif (
        isinstance(expression, exp.AlterTable)
        and (sexp := expression.find(exp.Set))
        and not sexp.args["tag"]
        and (eq := sexp.find(exp.EQ))
        and (eid := eq.find(exp.Identifier))
        and isinstance(eid.this, str)
        and eid.this.upper() == "COMMENT"
        and (lit := eq.find(exp.Literal))
        and (table := expression.find(exp.Table))
    ):
        new = SUCCESS_NOP.copy()
        new.args["table_comment"] = (table, lit.this)
        return new

    return expression


def extract_text_length(expression: exp.Expression) -> exp.Expression:
    """Extract length of text columns.

    duckdb doesn't have fixed-sized text types. So we capture the size of text types and store that in the
    character_maximum_length arg.

    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The original expression, with any text lengths stored in the new 'text_lengths' arg.
    """

    if isinstance(expression, (exp.Create, exp.AlterTable)):
        text_lengths = []
        for dt in expression.find_all(exp.DataType):
            if dt.this in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT):
                col_name = dt.parent and dt.parent.this and dt.parent.this.this
                if dt_size := dt.find(exp.DataTypeParam):
                    size = (
                        isinstance(dt_size.this, exp.Literal)
                        and isinstance(dt_size.this.this, str)
                        and int(dt_size.this.this)
                    )
                else:
                    size = 16777216
                text_lengths.append((col_name, size))

        if text_lengths:
            expression.args["text_lengths"] = text_lengths

    return expression


def flatten(expression: exp.Expression) -> exp.Expression:
    """Flatten an array.

    See https://docs.snowflake.com/en/sql-reference/functions/flatten

    TODO: return index.
    TODO: support objects.
    """
    if (
        isinstance(expression, exp.Lateral)
        and isinstance(expression.this, exp.Explode)
        and (alias := expression.args.get("alias"))
        # always true; when no explicit alias provided this will be _flattened
        and isinstance(alias, exp.TableAlias)
    ):
        explode_expression = expression.this.this.expression

        return exp.Lateral(
            this=exp.Unnest(
                expressions=[
                    exp.Cast(
                        this=explode_expression,
                        to=exp.DataType(
                            this=exp.DataType.Type.ARRAY,
                            expressions=[exp.DataType(this=exp.DataType.Type.JSON, nested=False, prefix=False)],
                            nested=True,
                        ),
                    )
                ],
            ),
            alias=exp.TableAlias(this=alias.this, columns=[exp.Identifier(this="VALUE", quoted=False)]),
        )

    return expression


def float_to_double(expression: exp.Expression) -> exp.Expression:
    """Convert float to double for 64 bit precision.

    Snowflake floats are all 64 bit (ie: double)
    see https://docs.snowflake.com/en/sql-reference/data-types-numeric#float-float4-float8
    """

    if isinstance(expression, exp.DataType) and expression.this == exp.DataType.Type.FLOAT:
        expression.args["this"] = exp.DataType.Type.DOUBLE

    return expression


def identifier(expression: exp.Expression) -> exp.Expression:
    """Convert identifier function to an identifier.

    See https://docs.snowflake.com/en/sql-reference/identifier-literal
    """

    if (
        isinstance(expression, exp.Anonymous)
        and isinstance(expression.this, str)
        and expression.this.upper() == "IDENTIFIER"
    ):
        expression = exp.Identifier(this=expression.expressions[0].this, quoted=False)

    return expression


def indices_to_json_extract(expression: exp.Expression) -> exp.Expression:
    """Convert indices on objects and arrays to json_extract.

    Supports Snowflake array indices, see
    https://docs.snowflake.com/en/sql-reference/data-types-semistructured#accessing-elements-of-an-array-by-index-or-by-slice
    and object indices, see
    https://docs.snowflake.com/en/sql-reference/data-types-semistructured#accessing-elements-of-an-object-by-key

    Duckdb uses the -> operator, aka the json_extract function, see
    https://duckdb.org/docs/extensions/json#json-extraction-functions

    This works for Snowflake arrays too because we convert them to JSON in duckdb.
    """
    if (
        isinstance(expression, exp.Bracket)
        and len(expression.expressions) == 1
        and (index := expression.expressions[0])
        and isinstance(index, exp.Literal)
        and index.this
    ):
        if index.is_string:
            return exp.JSONExtract(this=expression.this, expression=exp.Literal(this=f"$.{index.this}", is_string=True))
        else:
            return exp.JSONExtract(
                this=expression.this, expression=exp.Literal(this=f"$[{index.this}]", is_string=True)
            )

    return expression


def information_schema_fs_columns_snowflake(expression: exp.Expression) -> exp.Expression:
    """Redirect to the information_schema._fs_columns_snowflake view which has metadata that matches snowflake.

    Because duckdb doesn't store character_maximum_length or character_octet_length.
    """

    if (
        isinstance(expression, exp.Select)
        and (tbl_exp := expression.find(exp.Table))
        and tbl_exp.name.upper() == "COLUMNS"
        and tbl_exp.db.upper() == "INFORMATION_SCHEMA"
    ):
        tbl_exp.set("this", exp.Identifier(this="_FS_COLUMNS_SNOWFLAKE", quoted=False))

    return expression


def information_schema_fs_tables_ext(expression: exp.Expression) -> exp.Expression:
    """Join to information_schema._fs_tables_ext to access additional metadata columns (eg: comment)."""

    if (
        isinstance(expression, exp.Select)
        and (tbl_exp := expression.find(exp.Table))
        and tbl_exp.name.upper() == "TABLES"
        and tbl_exp.db.upper() == "INFORMATION_SCHEMA"
    ):
        return expression.join(
            "information_schema._fs_tables_ext",
            on=(
                """
                tables.table_catalog = _fs_tables_ext.ext_table_catalog AND
                tables.table_schema = _fs_tables_ext.ext_table_schema AND
                tables.table_name = _fs_tables_ext.ext_table_name
                """
            ),
            join_type="left",
        )

    return expression


def integer_precision(expression: exp.Expression) -> exp.Expression:
    """Convert integers to bigint.

    So dataframes will return them with a dtype of int64.
    """

    if (
        isinstance(expression, exp.DataType)
        and (expression.this == exp.DataType.Type.DECIMAL and not expression.expressions)
        or expression.this in (exp.DataType.Type.INT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT)
    ):
        return exp.DataType(
            this=exp.DataType.Type.BIGINT,
            nested=False,
            prefix=False,
        )

    return expression


def json_extract_cased_as_varchar(expression: exp.Expression) -> exp.Expression:
    """Convert json to varchar inside JSONExtract.

    Snowflake case conversion (upper/lower) turns variant into varchar. This
    mimics that behaviour within get_path.

    TODO: a generic version that works on any variant, not just JSONExtract

    Returns a raw string using the Duckdb ->> operator, aka the json_extract_string function, see
    https://duckdb.org/docs/extensions/json#json-extraction-functions
    """
    if (
        isinstance(expression, (exp.Upper, exp.Lower))
        and (gp := expression.this)
        and isinstance(gp, exp.JSONExtract)
        and (path := gp.expression)
        and isinstance(path, exp.JSONPath)
    ):
        expression.set("this", exp.JSONExtractScalar(this=gp.this, expression=path))

    return expression


def json_extract_cast_as_varchar(expression: exp.Expression) -> exp.Expression:
    """Return raw unquoted string when casting json extraction to varchar.

    Returns a raw string using the Duckdb ->> operator, aka the json_extract_string function, see
    https://duckdb.org/docs/extensions/json#json-extraction-functions
    """
    if (
        isinstance(expression, exp.Cast)
        and (to := expression.to)
        and isinstance(to, exp.DataType)
        and to.this in {exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT}
        and (je := expression.this)
        and isinstance(je, exp.JSONExtract)
        and (path := je.expression)
        and isinstance(path, exp.JSONPath)
    ):
        return exp.JSONExtractScalar(this=je.this, expression=path)
    return expression


def json_extract_precedence(expression: exp.Expression) -> exp.Expression:
    """Associate json extract operands to avoid duckdb operators of higher precedence transforming the expression.

    See https://github.com/tekumara/fakesnow/issues/53
    """
    if isinstance(expression, exp.JSONExtract):
        return exp.Paren(this=expression)
    return expression


def json_extract_eq_in_literal_string_cast_varchar(expression: exp.Expression) -> exp.Expression:
    """
    Snowflake casts value of semi structured access to string implicitly if
    it's an operand in an _equality_ or _IN_ expression against a _literal_
    string/strings.

    DuckDB tries to cast literal string to JSON;
        SELECT TO_JSON({'K': 10}) AS D, ( D -> '$.k') = 'SomeString' AS EQ, ( D -> '$.k') IN ('SomeString') AS INN
            Error: Conversion Error: Malformed JSON at byte 0 of input: unexpected character.  Input: SomeString

    Snowflake;
        SELECT {'k': 'SomeString'} AS d, d:k = 'SomeString' as eq, d:k IN ('SomeString') AS INN;
            D: {"k": 'SomeString'}
            EQ: TRUE
            INN: TRUE


    Snowflake;
        data:value = 'foo'
        data:value IN ('foo', 'bar')

    DuckDB;
        (data ->> '$.value') = 'foo'
        data ->> '$.value' IN ('foo', 'bar')

    """

    def unwrap_paren(expression: exp.Expression) -> tuple[exp.Expression, bool]:
        if isinstance(expression, exp.Paren):
            r, _ = unwrap_paren(expression.this)
            return r, True

        return expression, False

    def is_json_extract(expression: exp.Expression) -> bool:
        return (
            isinstance(expression, exp.JSONExtract)
            and (je := expression)
            and (path := je.expression)
            and isinstance(path, exp.JSONPath)
        )

    def to_json_extract_scalar(expression: exp.JSONExtract) -> exp.Expression:
        return exp.JSONExtractScalar(this=expression.this, expression=expression.expression)

    if isinstance(expression, exp.EQ):
        left = expression.left
        right = expression.right

        left_unwrapped, left_had_paren = unwrap_paren(left)
        right_unwrapped, right_had_paren = unwrap_paren(right)
        if is_json_extract(left_unwrapped) and isinstance(right_unwrapped, exp.Literal) and right_unwrapped.is_string:
            json_extract_scalar = exp.Paren(this=to_json_extract_scalar(cast(exp.JSONExtract, left_unwrapped)))
            extract = exp.Paren(this=json_extract_scalar) if left_had_paren else json_extract_scalar
            literal = exp.Paren(this=right_unwrapped) if right_had_paren else right_unwrapped

            eq = exp.EQ(this=extract, expression=literal)
            if expression.parent and isinstance(expression.parent, (exp.And, exp.Or)):
                return exp.Paren(this=eq)

            return eq

        if is_json_extract(right_unwrapped) and isinstance(left_unwrapped, exp.Literal) and left_unwrapped.is_string:
            json_extract_scalar = exp.Paren(this=to_json_extract_scalar(cast(exp.JSONExtract, right_unwrapped)))
            extract = exp.Paren(this=json_extract_scalar) if right_had_paren else json_extract_scalar
            literal = exp.Paren(this=left_unwrapped) if left_had_paren else left_unwrapped

            eq = exp.EQ(this=literal, expression=extract)
            # If parent is AND or OR, wrap in parenthesis to avoid precedence issues.
            if expression.parent and isinstance(expression.parent, (exp.And, exp.Or)):
                return exp.Paren(this=eq)

            return eq

        return expression

    if not isinstance(expression, exp.In):
        return expression

    # if left is json extract and right is a list of string literals
    left_unwrapped, had_parens = unwrap_paren(expression.this)
    if is_json_extract(left_unwrapped) and all(
        isinstance(e, exp.Literal) and e.is_string for e in (expression.expressions)
    ):
        json_extract_scalar = to_json_extract_scalar(cast(exp.JSONExtract, left_unwrapped))
        extract = exp.Paren(this=json_extract_scalar) if had_parens else json_extract_scalar

        inn = exp.In(this=extract, expressions=expression.expressions)
        # If parent is AND or OR, wrap in parenthesis to avoid precedence issues.
        if expression.parent and isinstance(expression.parent, (exp.And, exp.Or)):
            return exp.Paren(this=inn)
        return inn

    return expression


def trim_cast_varchar(expression: exp.Expression) -> exp.Expression:
    """Snowflake's TRIM casts input to VARCHAR implicitly."""

    if not (isinstance(expression, exp.Trim)):
        return expression

    operand = expression.this
    if isinstance(operand, exp.Cast) and operand.to.this in [exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT]:
        return expression

    return exp.Trim(
        this=exp.Cast(this=operand, to=exp.DataType(this=exp.DataType.Type.VARCHAR, nested=False, prefix=False))
    )


def random(expression: exp.Expression) -> exp.Expression:
    """Convert random() and random(seed).

    Snowflake random() is an signed 64 bit integer.
    Duckdb random() is a double between 0 and 1 and uses setseed() to set the seed.
    """
    if isinstance(expression, exp.Select) and (rand := expression.find(exp.Rand)):
        # shift result to between min and max signed 64bit integer
        new_rand = exp.Cast(
            this=exp.Paren(
                this=exp.Mul(
                    this=exp.Paren(this=exp.Sub(this=exp.Rand(), expression=exp.Literal(this=0.5, is_string=False))),
                    expression=exp.Literal(this=9223372036854775807, is_string=False),
                )
            ),
            to=exp.DataType(this=exp.DataType.Type.BIGINT, nested=False, prefix=False),
        )

        rand.replace(new_rand)

        # convert seed to double between 0 and 1 by dividing by max INTEGER (int32)
        # (not max BIGINT (int64) because we don't have enough floating point precision to distinguish seeds)
        # then attach to SELECT as the seed arg
        # (we can't attach it to exp.Rand because it will be rendered in the sql)
        if rand.this and isinstance(rand.this, exp.Literal):
            expression.args["seed"] = f"{rand.this}/2147483647-0.5"

    return expression


def sample(expression: exp.Expression) -> exp.Expression:
    if isinstance(expression, exp.TableSample) and not expression.args.get("method"):
        # set snowflake default (bernoulli) rather than use the duckdb default (system)
        # because bernoulli works better at small row sizes like we have in tests
        expression.set("method", exp.Var(this="BERNOULLI"))

    return expression


def object_construct(expression: exp.Expression) -> exp.Expression:
    """Convert OBJECT_CONSTRUCT to TO_JSON.

    Internally snowflake stores OBJECT types as a json string, so the Duckdb JSON type most closely matches.

    See https://docs.snowflake.com/en/sql-reference/functions/object_construct
    """

    if isinstance(expression, exp.Struct):
        non_null_expressions = []
        for e in expression.expressions:
            if not (isinstance(e, exp.PropertyEQ)):
                non_null_expressions.append(e)
                continue

            left = e.left
            right = e.right

            left_is_null = isinstance(left, exp.Null)
            right_is_null = isinstance(right, exp.Null)

            if left_is_null or right_is_null:
                continue

            non_null_expressions.append(e)

        new_struct = expression.copy()
        new_struct.set("expressions", non_null_expressions)
        return exp.Anonymous(this="TO_JSON", expressions=[new_struct])
    return expression


def to_variant(expression: exp.Expression) -> exp.Expression:
    """Convert to_variant to to_json.
    See https://docs.snowflake.com/en/sql-reference/functions/to_variant

    There're not any type on DuckDB to resemble VARIANT. VARIANT can store
    arbitrary data types including structured data. Regardless on underlyin
    data type, VARIANT columns enable structured access;
    SELECT TO_VARIANT('hello') AS D, TYPEOF(D), D:SOMEFIELD;
        D       	 TYPEOF(D) 	 D:SOMEFIELD
        "hello" 	 VARCHAR


    No equivalent in DuckDB, so we use JSON instead.
    """

    if isinstance(expression, exp.Anonymous) and expression.this.upper() == "TO_VARIANT":
        new = expression.copy()
        new.args["this"] = "TO_JSON"
        return new

    return expression


def try_parse_json(expression: exp.Expression) -> exp.Expression:
    """Convert tr_parse_json() to try_cast(... as JSON).

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("select try_parse_json('{}')").transform(parse_json).sql()
        "SELECT TRY_CAST('{}' AS JSON)"
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    if (
        isinstance(expression, exp.Anonymous)
        and isinstance(expression.this, str)
        and expression.this.upper() == "TRY_PARSE_JSON"
    ):
        expressions = expression.expressions
        return exp.TryCast(
            this=expressions[0],
            to=exp.DataType(this=exp.DataType.Type.JSON, nested=False),
        )

    return expression


def regex_replace(expression: exp.Expression) -> exp.Expression:
    """Transform regex_replace expressions from snowflake to duckdb."""

    if isinstance(expression, exp.RegexpReplace) and isinstance(expression.expression, exp.Literal):
        if len(expression.args) > 3:
            # see https://docs.snowflake.com/en/sql-reference/functions/regexp_replace
            raise NotImplementedError(
                "REGEXP_REPLACE with additional parameters (eg: <position>, <occurrence>, <parameters>) not supported"
            )

        # pattern: snowflake requires escaping backslashes in single-quoted string constants, but duckdb doesn't
        # see https://docs.snowflake.com/en/sql-reference/functions-regexp#label-regexp-escape-character-caveats
        expression.args["expression"] = exp.Literal(
            this=expression.expression.this.replace("\\\\", "\\"), is_string=True
        )

        if not expression.args.get("replacement"):
            # if no replacement string, the snowflake default is ''
            expression.args["replacement"] = exp.Literal(this="", is_string=True)

        # snowflake regex replacements are global
        expression.args["modifiers"] = exp.Literal(this="g", is_string=True)

    return expression


def regex_substr(expression: exp.Expression) -> exp.Expression:
    """Transform regex_substr expressions from snowflake to duckdb.

    See https://docs.snowflake.com/en/sql-reference/functions/regexp_substr
    """

    if isinstance(expression, exp.RegexpExtract):
        subject = expression.this

        # pattern: snowflake requires escaping backslashes in single-quoted string constants, but duckdb doesn't
        # see https://docs.snowflake.com/en/sql-reference/functions-regexp#label-regexp-escape-character-caveats
        pattern = expression.expression
        pattern.args["this"] = pattern.this.replace("\\\\", "\\")

        # number of characters from the beginning of the string where the function starts searching for matches
        try:
            position = expression.args["position"]
        except KeyError:
            position = exp.Literal(this="1", is_string=False)

        # which occurrence of the pattern to match
        try:
            occurrence = int(expression.args["occurrence"].this)
        except KeyError:
            occurrence = 1

        # the duckdb dialect increments bracket (ie: index) expressions by 1 because duckdb is 1-indexed,
        # so we need to compensate by subtracting 1
        occurrence = exp.Literal(this=str(occurrence - 1), is_string=False)

        try:
            regex_parameters_value = str(expression.args["parameters"].this)
            # 'e' parameter doesn't make sense for duckdb
            regex_parameters = exp.Literal(this=regex_parameters_value.replace("e", ""), is_string=True)
        except KeyError:
            regex_parameters = exp.Literal(is_string=True)

        try:
            group_num = expression.args["group"]
        except KeyError:
            if isinstance(regex_parameters.this, str) and "e" in regex_parameters.this:
                group_num = exp.Literal(this="1", is_string=False)
            else:
                group_num = exp.Literal(this="0", is_string=False)

        expression = exp.Bracket(
            this=exp.Anonymous(
                this="regexp_extract_all",
                expressions=[
                    # slice subject from position onwards
                    exp.Bracket(this=subject, expressions=[exp.Slice(this=position)]),
                    pattern,
                    group_num,
                    regex_parameters,
                ],
            ),
            # select index of occurrence
            expressions=[occurrence],
        )

    return expression


# TODO: move this into a Dialect as a transpilation
def set_schema(expression: exp.Expression, current_database: str | None) -> exp.Expression:
    """Transform USE SCHEMA/DATABASE to SET schema.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("USE SCHEMA bar").transform(set_schema, current_database="foo").sql()
        "SET schema = 'foo.bar'"
        >>> sqlglot.parse_one("USE SCHEMA foo.bar").transform(set_schema).sql()
        "SET schema = 'foo.bar'"
        >>> sqlglot.parse_one("USE DATABASE marts").transform(set_schema).sql()
        "SET schema = 'marts.main'"

        See tests for more examples.
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: A SET schema expression if the input is a USE
            expression, otherwise expression is returned as-is.
    """

    if (
        isinstance(expression, exp.Use)
        and (kind := expression.args.get("kind"))
        and isinstance(kind, exp.Var)
        and kind.name
        and kind.name.upper() in ["SCHEMA", "DATABASE"]
    ):
        assert expression.this, f"No identifier for USE expression {expression}"

        if kind.name.upper() == "DATABASE":
            # duckdb's default schema is main
            name = f"{expression.this.name}.main"
        else:
            # SCHEMA
            if db := expression.this.args.get("db"):  # noqa: SIM108
                db_name = db.name
            else:
                # isn't qualified with a database
                db_name = current_database or MISSING_DATABASE

            name = f"{db_name}.{expression.this.name}"

        return exp.Command(this="SET", expression=exp.Literal.string(f"schema = '{name}'"))

    return expression


def show_objects_tables(expression: exp.Expression, current_database: str | None = None) -> exp.Expression:
    """Transform SHOW OBJECTS/TABLES to a query against the information_schema.tables table.

    See https://docs.snowflake.com/en/sql-reference/sql/show-objects
        https://docs.snowflake.com/en/sql-reference/sql/show-tables
    """
    if not (
        isinstance(expression, exp.Show)
        and isinstance(expression.this, str)
        and (show := expression.this.upper())
        and show in {"OBJECTS", "TABLES"}
    ):
        return expression

    scope_kind = expression.args.get("scope_kind")
    table = expression.find(exp.Table)

    if scope_kind == "DATABASE":
        catalog = (table and table.name) or current_database
        schema = None
    elif scope_kind == "SCHEMA" and table:
        catalog = table.db or current_database
        schema = table.name
    else:
        # all objects / tables
        catalog = None
        schema = None

    tables_only = "table_type = 'BASE TABLE' and " if show == "TABLES" else ""
    exclude_fakesnow_tables = "not (table_schema == 'information_schema' and table_name like '_fs_%%')"
    # without a database will show everything in the "account"
    table_catalog = f" and table_catalog = '{catalog}'" if catalog else ""
    schema = f" and table_schema = '{schema}'" if schema else ""
    limit = limit.sql() if (limit := expression.args.get("limit")) and isinstance(limit, exp.Expression) else ""

    columns = [
        "to_timestamp(0)::timestamptz as 'created_on'",
        "table_name as 'name'",
        "case when table_type='BASE TABLE' then 'TABLE' else table_type end as 'kind'",
        "table_catalog as 'database_name'",
        "table_schema as 'schema_name'",
    ]

    terse = expression.args["terse"]
    if not terse:
        columns.append('null as "comment"')

    columns_str = ", ".join(columns)

    query = (
        f"SELECT {columns_str} from information_schema.tables "
        f"where {tables_only}{exclude_fakesnow_tables}{table_catalog}{schema}{limit}"
    )

    return sqlglot.parse_one(query, read="duckdb")


SQL_SHOW_SCHEMAS = """
select
    to_timestamp(0)::timestamptz as 'created_on',
    schema_name as 'name',
    NULL as 'kind',
    catalog_name as 'database_name',
    NULL as 'schema_name'
from information_schema.schemata
where catalog_name not in ('memory', 'system', 'temp') and schema_name not in ('main', 'pg_catalog')
"""


def show_schemas(expression: exp.Expression, current_database: str | None = None) -> exp.Expression:
    """Transform SHOW SCHEMAS to a query against the information_schema.schemata table.

    See https://docs.snowflake.com/en/sql-reference/sql/show-schemas
    """
    if isinstance(expression, exp.Show) and isinstance(expression.this, str) and expression.this.upper() == "SCHEMAS":
        if (ident := expression.find(exp.Identifier)) and isinstance(ident.this, str):
            database = ident.this
        else:
            database = current_database

        return sqlglot.parse_one(
            f"{SQL_SHOW_SCHEMAS} and catalog_name = '{database}'" if database else SQL_SHOW_SCHEMAS, read="duckdb"
        )

    return expression


def tag(expression: exp.Expression) -> exp.Expression:
    """Handle tags. Transfer tags into upserts of the tag table.

    duckdb doesn't support tags. In lieu of a full implementation, for now we make it a NOP.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("ALTER TABLE table1 SET TAG foo='bar'").transform(tag).sql()
        "SELECT 'Statement executed successfully.'"
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    if isinstance(expression, exp.AlterTable) and (actions := expression.args.get("actions")):
        for a in actions:
            if isinstance(a, exp.Set) and a.args["tag"]:
                return SUCCESS_NOP
    elif (
        isinstance(expression, exp.Command)
        and (cexp := expression.args.get("expression"))
        and isinstance(cexp, str)
        and "SET TAG" in cexp.upper()
    ):
        # alter table modify column set tag
        return SUCCESS_NOP

    return expression


def to_date(expression: exp.Expression) -> exp.Expression:
    """Convert to_date() to a cast.

    See https://docs.snowflake.com/en/sql-reference/functions/to_date

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("SELECT to_date(to_timestamp(0))").transform(to_date).sql()
        "SELECT CAST(DATE_TRUNC('day', TO_TIMESTAMP(0)) AS DATE)"
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    def _is_to_date(expression: exp.Expression) -> bool:
        return (
            isinstance(expression, exp.Anonymous)
            and isinstance(expression.this, str)
            and expression.this.upper() == "TO_DATE"
        )

    if _is_to_date(expression):
        # NOTE(selman):  There're cases were dbt generated queries would have TO_DATE(TO_DATE(...)).
        e = expression.expressions[0] if _is_to_date(expression.expressions[0]) else expression
        print("GOT TO_DATE")
        return exp.Cast(
            this=e.expressions[0],
            to=exp.DataType(this=exp.DataType.Type.DATE, nested=False, prefix=False),
        )
    return expression


def to_decimal(expression: exp.Expression) -> exp.Expression:
    """Transform to_decimal, to_number, to_numeric expressions from snowflake to duckdb.

    See https://docs.snowflake.com/en/sql-reference/functions/to_decimal
    """

    if (
        isinstance(expression, exp.Anonymous)
        and isinstance(expression.this, str)
        and expression.this.upper() in ["TO_DECIMAL", "TO_NUMBER", "TO_NUMERIC"]
    ):
        expressions: list[exp.Expression] = expression.expressions

        if len(expressions) > 1 and expressions[1].is_string:
            # see https://docs.snowflake.com/en/sql-reference/functions/to_decimal#arguments
            raise NotImplementedError(f"{expression.this} with format argument")

        precision = expressions[1] if len(expressions) > 1 else exp.Literal(this="38", is_string=False)
        scale = expressions[2] if len(expressions) > 2 else exp.Literal(this="0", is_string=False)

        return exp.Cast(
            this=expressions[0],
            to=exp.DataType(this=exp.DataType.Type.DECIMAL, expressions=[precision, scale], nested=False, prefix=False),
        )

    return expression


def try_to_decimal(expression: exp.Expression) -> exp.Expression:
    """Transform try_to_decimal, try_to_number, try_to_numeric expressions from snowflake to duckdb.

    See https://docs.snowflake.com/en/sql-reference/functions/try_to_decimal
    """

    if (
        isinstance(expression, exp.Anonymous)
        and isinstance(expression.this, str)
        and expression.this.upper() in ["TRY_TO_DECIMAL", "TRY_TO_NUMBER", "TRY_TO_NUMERIC"]
    ):
        expressions: list[exp.Expression] = expression.expressions

        if len(expressions) > 1 and expressions[1].is_string:
            # see https://docs.snowflake.com/en/sql-reference/functions/to_decimal#arguments
            raise NotImplementedError(f"{expression.this} with format argument")

        precision = expressions[1] if len(expressions) > 1 else exp.Literal(this="38", is_string=False)
        scale = expressions[2] if len(expressions) > 2 else exp.Literal(this="0", is_string=False)

        return exp.TryCast(
            this=expressions[0],
            to=exp.DataType(this=exp.DataType.Type.DECIMAL, expressions=[precision, scale], nested=False, prefix=False),
        )

    return expression


def to_timestamp(expression: exp.Expression) -> exp.Expression:
    """Convert to_timestamp(seconds) to timestamp without timezone (ie: TIMESTAMP_NTZ).

    See https://docs.snowflake.com/en/sql-reference/functions/to_timestamp
    """
    if isinstance(expression, exp.UnixToTime):
        return exp.Cast(
            this=expression,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        )

    # NOTE(selman): sqlglot parses `to_timetamp(<non-literal-expression>)` as
    # TimeStrToTime and transpiles it to `CAST(col AS TIMESTAMP)`. In our case
    # all `to_timestamp(<non-literal-expression>)` calls are made with a
    # NUMERIC value, therefore needs to be transpiled to
    # `TO_TIMESTAMP(<numeric-expression>)` in DuckDB.
    # `TO_TIMESTAMP` returns TIMESTAMPTZ, as above, needs to be cast to TIMESTAMP.
    if isinstance(expression, exp.TimeStrToTime) and not isinstance(expression.this, exp.Literal):
        # we got a non-literal expression, so we need to transpile it to a UnixToTime
        e = exp.UnixToTime(this=expression.this)
        return exp.Cast(
            this=e,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        )

    return expression


def to_timestamp_ntz(expression: exp.Expression) -> exp.Expression:
    """Convert to_timestamp_ntz to to_timestamp (StrToTime).

    Because it's not yet supported by sqlglot, see https://github.com/tobymao/sqlglot/issues/2748
    """

    if isinstance(expression, exp.Anonymous) and (
        isinstance(expression.this, str) and expression.this.upper() == "TO_TIMESTAMP_NTZ"
    ):
        return exp.StrToTime(
            this=expression.expressions[0],
            format=exp.Literal(this="%Y-%m-%d %H:%M:%S", is_string=True),
        )
    return expression


def timestamp_ntz_ns(expression: exp.Expression) -> exp.Expression:
    """Convert timestamp_ntz(9) to timestamp_ntz.

    To compensate for https://github.com/duckdb/duckdb/issues/7980
    """

    if (
        isinstance(expression, exp.DataType)
        and expression.this == exp.DataType.Type.TIMESTAMP
        and exp.DataTypeParam(this=exp.Literal(this="9", is_string=False)) in expression.expressions
    ):
        new = expression.copy()
        del new.args["expressions"]
        return new

    return expression


def current_timestamp(expression: exp.Expression) -> exp.Expression:
    """Convert current_timestamp to CAST( current_timestamp AS TIMESTAMP ).

    `current_timestamp` in DuckDB returns TIMESTAMP WITH TIME ZONE. With other
    transformations, all timestamps are handled as TIMESTAMP.
    """

    if isinstance(expression, exp.CurrentTimestamp):
        return exp.Cast(
            this=expression,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        )

    return expression


def array_agg_within_group(expression: exp.Expression) -> exp.Expression:
    """Convert ARRAY_AGG(<expr>) WITHIN GROUP (<order-by-clause>) to ARRAY_AGG( <expr> <order-by-clause> )

    Snowflake uses ARRAY_AGG(<expr>) WITHIN GROUP (ORDER BY <order-by-clause>)
    to order the array, but DuckDB uses ARRAY_AGG( <expr> <order-by-clause> ).

    See;
        - https://docs.snowflake.com/en/sql-reference/functions/array_agg
        - https://duckdb.org/docs/sql/aggregates.html#order-by-clause-in-aggregate-functions

    Note; Snowflake has following restriction;
            If you specify DISTINCT and WITHIN GROUP, both must refer to the same column.
          Transformation does not handle this restriction.
    """
    if (
        isinstance(expression, exp.WithinGroup)
        and (agg := expression.find(exp.ArrayAgg))
        and (order := expression.expression)
    ):
        return exp.ArrayAgg(
            this=exp.Order(
                this=agg.this,
                expressions=order.expressions,
            )
        )

    return expression


# sqlglot.parse_one("create table example(date TIMESTAMP_NTZ(9));", read="snowflake")
def semi_structured_types(expression: exp.Expression) -> exp.Expression:
    """Convert OBJECT, ARRAY, and VARIANT types to duckdb compatible types.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("CREATE TABLE table1 (name object)").transform(semi_structured_types).sql()
        "CREATE TABLE table1 (name JSON)"
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    if isinstance(expression, exp.DataType) and expression.this in [
        exp.DataType.Type.ARRAY,
        exp.DataType.Type.OBJECT,
        exp.DataType.Type.VARIANT,
    ]:
        new = expression.copy()
        new.args["this"] = exp.DataType.Type.JSON
        return new

    return expression


def upper_case_unquoted_identifiers(expression: exp.Expression) -> exp.Expression:
    """Upper case unquoted identifiers.

    Snowflake represents case-insensitivity using upper-case identifiers in cursor results.
    duckdb uses lowercase. We convert all unquoted identifiers to uppercase to match snowflake.

    Example:
        >>> import sqlglot
        >>> sqlglot.parse_one("select name, name as fname from table1").transform(upper_case_unquoted_identifiers).sql()
        'SELECT NAME, NAME AS FNAME FROM TABLE1'
    Args:
        expression (exp.Expression): the expression that will be transformed.

    Returns:
        exp.Expression: The transformed expression.
    """

    if isinstance(expression, exp.Identifier) and not expression.quoted and isinstance(expression.this, str):
        new = expression.copy()
        new.set("this", expression.this.upper())
        return new

    return expression


def timeadd(expression: exp.Expression) -> exp.Expression:
    """TIMEADD is an alias for DATEADD
    See: https://docs.snowflake.com/en/sql-reference/functions/timeadd
    """

    # SELECT
    #   TIMEADD('hour', 7, CURRENT_DATE)
    # Select(
    #   expressions=[
    #     Anonymous(
    #       this=timeadd,
    #       expressions=[
    #         Literal(this=hour, is_string=True),
    #         Literal(this=7, is_string=False),
    #         CurrentDate()])])

    # SELECT
    #   TIMEADD("hour", 7, CURRENT_DATE)
    # Select(
    #   expressions=[
    #     Anonymous(
    #       this=timeadd,
    #       expressions=[
    #         Column(
    #           this=Identifier(this=hour, quoted=True)),
    #         Literal(this=7, is_string=False),
    #         CurrentDate()])])

    # SELECT
    #   TIMEADD(HOUR, 7, CURRENT_DATE)
    # Select(
    #   expressions=[
    #     Anonymous(
    #       this=timeadd,
    #       expressions=[
    #         Column(
    #           this=Identifier(this=HOUR, quoted=False)),
    #         Literal(this=7, is_string=False),
    #         CurrentDate()])])

    # SELECT
    #   CURRENT_DATE + INTERVAL 7 HOUR
    # Select(
    #   expressions=[
    #     DateAdd(
    #       this=CurrentDate(),
    #       expression=Literal(this=7, is_string=False),
    #       unit=Var(this=HOUR))])

    if not (
        isinstance(expression, exp.Anonymous)
        and isinstance(expression.this, str)
        and expression.this.upper() == "TIMEADD"
    ):
        return expression

    if len(expression.expressions) != 3:
        return expression

    unit, value, date = expression.expressions.copy()

    if isinstance(unit, exp.Literal):
        unit = exp.Var(this=unit.this.upper())
    elif isinstance(unit, exp.Column) and isinstance(unit.this, exp.Identifier):
        unit = exp.Var(this=unit.this.this.upper())
    else:
        return expression

    return exp.DateAdd(this=date, expression=value, unit=unit)


def dateadd_literal_date_cast(expression: exp.Expression) -> exp.Expression:
    """Cast result of DATEADD to DATE if the given expression is cast to date
    from a string literal and unit is either DAY or WEEK.

    Snowflake;
        SELECT DATEADD(DAY, 3, '2023-03-03'::DATE) as D;
            D: 2023-03-06 (DATE)
    DuckDB;
        SELECT CAST('2023-03-03' AS DATE) + INTERVAL 3 DAY AS D
        D: 2023-03-06 00:00:00 (TIMESTAMP)
    """

    if not isinstance(expression, exp.DateAdd):
        return expression

    dateadd = expression

    if dateadd.unit is None or dateadd.unit.this not in {"DAY", "WEEK"}:
        return expression

    if not isinstance(dateadd.this, exp.Cast):
        return expression

    cast = dateadd.this

    if not isinstance(cast.this, exp.Literal) or not cast.this.is_string or cast.to.this != exp.DataType.Type.DATE:
        return expression

    return exp.Cast(
        this=expression,
        to=exp.DataType(this=exp.DataType.Type.DATE, nested=False, prefix=False),
    )


def dateadd_literal_date_string(expression: exp.Expression) -> exp.Expression:
    """TODO(selman):: TBD
    Snowflake;
        SELECT DATEADD(DAY, 3, '2023-03-03') AS D;
            D: 2023-03-06 00:00:00.000 +0000 (TIMESTAMP_{NTZ,LTZ})

    Want:
        SELECT '2023-03-03'::DATE + INTERVAL 3 DAY AS D
            D: 2023-03-06 00:00:00 (TIMESTAMP)
    """

    if not isinstance(expression, exp.DateAdd):
        return expression

    dateadd = expression

    if not isinstance(dateadd.this, exp.Literal) or not dateadd.this.is_string:
        return expression

    literal = dateadd.this

    # Regardless of unit, if expression is a string literal, cast it to
    # timestamp
    new_dateadd = dateadd.copy()
    new_dateadd.set(
        "this",
        exp.Cast(
            this=literal,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        ),
    )

    return new_dateadd


def datediff_literal_cast(expression: exp.Expression) -> exp.Expression:
    """TODO(selman):: TBD
    Casts string literal operands of DATEDIFF to TIMESTAMP.

    DateDiff(
        this=Literal(this=2023-04-02, is_string=True),
        expression=Column(
            this=Identifier(this=LAST_PURCHASE_DATE, quoted=False)
        ),
        unit=Var(this=DAY)
    )


    DateDiff(
        this=Column(
            this=Identifier(this=LAST_PURCHASE_DATE, quoted=False)
        ),
        expression=Literal(this=2023-04-02, is_string=True),
        unit=Var(this=DAY)
    )

    DateDiff(
        this=Literal(this=2203-04-02, is_string=True),
        expression=Literal(this=2023-04-02, is_string=True),
        unit=Var(this=DAY)
    )
    """

    if not isinstance(expression, exp.DateDiff):
        return expression

    datediff = expression
    op1 = datediff.this.copy()
    op2 = datediff.expression.copy()

    if isinstance(op1, exp.Literal) and op1.is_string:
        op1 = exp.Cast(
            this=op1,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        )

    if isinstance(op2, exp.Literal) and op2.is_string:
        op2 = exp.Cast(
            this=op2,
            to=exp.DataType(this=exp.DataType.Type.TIMESTAMP, nested=False, prefix=False),
        )

    new_datediff = datediff.copy()
    new_datediff.set("this", op1)
    new_datediff.set("expression", op2)

    return new_datediff


def values_columns(expression: exp.Expression) -> exp.Expression:
    """Support column1, column2 expressions in VALUES.

    Snowflake uses column1, column2 .. for unnamed columns in VALUES. Whereas duckdb uses col0, col1 ..
    See https://docs.snowflake.com/en/sql-reference/constructs/values#examples
    """

    if (
        isinstance(expression, exp.Values)
        and not expression.alias
        and expression.find_ancestor(exp.Select)
        and (values := expression.find(exp.Tuple))
    ):
        num_columns = len(values.expressions)
        columns = [exp.Identifier(this=f"COLUMN{i + 1}", quoted=True) for i in range(num_columns)]
        expression.set("alias", exp.TableAlias(this=exp.Identifier(this="_", quoted=False), columns=columns))

    return expression


def show_users(expression: exp.Expression) -> exp.Expression:
    """Transform SHOW USERS to a query against the global database's information_schema._fs_users table.

    https://docs.snowflake.com/en/sql-reference/sql/show-users
    """
    if isinstance(expression, exp.Show) and isinstance(expression.this, str) and expression.this.upper() == "USERS":
        return sqlglot.parse_one(f"SELECT * FROM {USERS_TABLE_FQ_NAME}", read="duckdb")

    return expression


def create_user(expression: exp.Expression) -> exp.Expression:
    """Transform CREATE USER to a query against the global database's information_schema._fs_users table.

    https://docs.snowflake.com/en/sql-reference/sql/create-user
    """
    # XXX: this is a placeholder. We need to implement the full CREATE USER syntax, but
    #      sqlglot doesnt yet support Create for snowflake.
    if isinstance(expression, exp.Command) and expression.this == "CREATE":
        sub_exp = expression.expression.strip()
        if sub_exp.upper().startswith("USER"):
            _, name, *ignored = sub_exp.split(" ")
            if ignored:
                raise NotImplementedError(f"`CREATE USER` with {ignored} not yet supported")
            return sqlglot.parse_one(f"INSERT INTO {USERS_TABLE_FQ_NAME} (name) VALUES ('{name}')", read="duckdb")

    return expression


def show_keys(
    expression: exp.Expression,
    current_database: str | None = None,
    *,
    kind: Literal["PRIMARY", "UNIQUE", "FOREIGN"],
) -> exp.Expression:
    """Transform SHOW <kind> KEYS to a query against the duckdb_constraints meta-table.

    https://docs.snowflake.com/en/sql-reference/sql/show-primary-keys
    """
    snowflake_kind = kind
    if kind == "FOREIGN":
        snowflake_kind = "IMPORTED"

    if (
        isinstance(expression, exp.Show)
        and isinstance(expression.this, str)
        and expression.this.upper() == f"{snowflake_kind} KEYS"
    ):
        if kind == "FOREIGN":
            statement = f"""
                SELECT
                    to_timestamp(0)::timestamptz as created_on,

                    '' as pk_database_name,
                    '' as pk_schema_name,
                    '' as pk_table_name,
                    '' as pk_column_name,
                    unnest(constraint_column_names) as pk_column_name,

                    database_name as fk_database_name,
                    schema_name as fk_schema_name,
                    table_name as fk_table_name,
                    unnest(constraint_column_names) as fk_column_name,
                    1 as key_sequence,
                    'NO ACTION' as update_rule,
                    'NO ACTION' as delete_rule,
                    LOWER(CONCAT(database_name, '_', schema_name, '_', table_name, '_pkey')) AS fk_name,
                    LOWER(CONCAT(database_name, '_', schema_name, '_', table_name, '_pkey')) AS pk_name,
                    'NOT DEFERRABLE' as deferrability,
                    'false' as rely,
                    null as "comment"
                FROM duckdb_constraints
                WHERE constraint_type = 'PRIMARY KEY'
                  AND database_name = '{current_database}'
                  AND table_name NOT LIKE '_fs_%'
                """
        else:
            statement = f"""
                SELECT
                    to_timestamp(0)::timestamptz as created_on,
                    database_name as database_name,
                    schema_name as schema_name,
                    table_name as table_name,
                    unnest(constraint_column_names) as column_name,
                    1 as key_sequence,
                    LOWER(CONCAT(database_name, '_', schema_name, '_', table_name, '_pkey')) AS constraint_name,
                    'false' as rely,
                    null as "comment"
                FROM duckdb_constraints
                WHERE constraint_type = '{kind} KEY'
                  AND database_name = '{current_database}'
                  AND table_name NOT LIKE '_fs_%'
                """

        scope_kind = expression.args.get("scope_kind")
        if scope_kind:
            table = expression.args["scope"]

            if scope_kind == "SCHEMA":
                db = table and table.db
                schema = table and table.name
                if db:
                    statement += f"AND database_name = '{db}' "

                if schema:
                    statement += f"AND schema_name = '{schema}' "
            else:
                raise NotImplementedError(f"SHOW PRIMARY KEYS with {scope_kind} not yet supported")
        return sqlglot.parse_one(statement)
    return expression
