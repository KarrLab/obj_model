'''
:Author: Arthur Goldberg <Arthur.Goldberg@mssm.edu>
:Author: Jonathan Karr  <jonrkarr@gmail.com>
:Date: 2018-12-19
:Copyright: 2016-2018, Karr Lab
:License: MIT
'''
from enum import Enum
from obj_model.core import (Model, SlugAttribute, FloatAttribute, StringAttribute, EnumAttribute,
                            ManyToOneAttribute, ManyToManyAttribute,
                            InvalidObject, InvalidAttribute)
from obj_model.expression import (ExpressionOneToOneAttribute, ExpressionManyToOneAttribute,
                                  ExpressionStaticTermMeta, ExpressionDynamicTermMeta,
                                  ExpressionExpressionTermMeta,
                                  ObjModelTokenCodes, ObjModelToken, LexMatch,
                                  Expression, ParsedExpression,
                                  ParsedExpressionValidator, LinearParsedExpressionValidator,
                                  ParsedExpressionError)
from wc_utils.util.units import unit_registry
import mock
import re
import token
import unittest


class BaseModel(Model):
    id = SlugAttribute()


class Parameter(Model):
    id = SlugAttribute()
    model = ManyToOneAttribute(BaseModel, related_name='parameters')
    value = FloatAttribute()
    units = StringAttribute()

    class Meta(Model.Meta, ExpressionStaticTermMeta):
        expression_term_value = 'value'
        expression_term_units = 'units'


class Species(Model):
    id = StringAttribute(primary=True, unique=True)
    model = ManyToOneAttribute(BaseModel, related_name='species')
    units = StringAttribute()

    class Meta(Model.Meta, ExpressionDynamicTermMeta):
        expression_term_token_pattern = (token.NAME, token.LSQB, token.NAME, token.RSQB)
        expression_term_units = 'units'


class SubFunctionExpression(Model, Expression):
    expression = StringAttribute()
    parameters = ManyToManyAttribute(Parameter, related_name='sub_function_expressions')
    sub_functions = ManyToManyAttribute('SubFunction', related_name='sub_function_expressions')

    class Meta(Model.Meta, Expression.Meta):
        expression_term_models = ('SubFunction', 'Parameter',)

    def serialize(self): return Expression.serialize(self)

    @classmethod
    def deserialize(cls, value, objects): return Expression.deserialize(cls, value, objects)

    def validate(self): return Expression.validate(self, self.parent_sub_function)


class SubFunction(Model):
    id = SlugAttribute()
    model = ManyToOneAttribute(BaseModel, related_name='sub_functions')
    expression = ExpressionOneToOneAttribute(SubFunctionExpression, related_name='parent_sub_function')
    units = StringAttribute()

    class Meta(Model.Meta, ExpressionExpressionTermMeta):
        expression_term_model = SubFunctionExpression


class BooleanSubFunctionExpression(Model, Expression):
    expression = StringAttribute()
    parameters = ManyToManyAttribute(Parameter, related_name='boolean_sub_function_expressions')

    class Meta(Model.Meta, Expression.Meta):
        expression_term_models = ('Parameter',)

    def serialize(self): return Expression.serialize(self)

    @classmethod
    def deserialize(cls, value, objects): return Expression.deserialize(cls, value, objects)

    def validate(self): return Expression.validate(self, self.boolean_sub_function)


class BooleanUnit(int, Enum):
    dimensionless = 1


class BooleanSubFunction(Model):
    id = SlugAttribute()
    model = ManyToOneAttribute(BaseModel, related_name='boolean_sub_functions')
    expression = ExpressionOneToOneAttribute(BooleanSubFunctionExpression, related_name='boolean_sub_function')
    units = EnumAttribute(BooleanUnit, default=BooleanUnit.dimensionless)

    class Meta(Model.Meta, ExpressionExpressionTermMeta):
        expression_term_model = BooleanSubFunctionExpression


class LinearSubFunctionExpression(Model, Expression):
    expression = StringAttribute()
    parameters = ManyToManyAttribute(Parameter, related_name='linear_sub_function_expressions')

    class Meta(Model.Meta, Expression.Meta):
        expression_term_models = ('Parameter',)
        expression_is_linear = True

    def serialize(self): return Expression.serialize(self)

    @classmethod
    def deserialize(cls, value, objects): return Expression.deserialize(cls, value, objects)

    def validate(self): return Expression.validate(self, self.linear_sub_function)


class LinearSubFunction(Model):
    id = SlugAttribute()
    model = ManyToOneAttribute(BaseModel, related_name='linear_sub_functions')
    expression = ExpressionManyToOneAttribute(LinearSubFunctionExpression, related_name='linear_sub_function')
    units = StringAttribute()

    class Meta(Model.Meta, ExpressionExpressionTermMeta):
        expression_term_model = LinearSubFunctionExpression


class FunctionExpression(Model, Expression):
    expression = StringAttribute()
    sub_functions = ManyToManyAttribute(SubFunction, related_name='function_expressions')
    linear_sub_functions = ManyToManyAttribute(LinearSubFunction, related_name='function_expressions')
    parameters = ManyToManyAttribute(Parameter, related_name='function_expressions')
    species = ManyToManyAttribute(Species, related_name='function_expressions')

    class Meta(Model.Meta, Expression.Meta):
        expression_term_models = ('SubFunction', 'LinearSubFunction', 'Parameter', 'Species')
        expression_type = float

    def serialize(self): return Expression.serialize(self)

    @classmethod
    def deserialize(cls, value, objects): return Expression.deserialize(cls, value, objects)

    def validate(self): return Expression.validate(self, self.function)


class Function(Model):
    id = SlugAttribute()
    model = ManyToOneAttribute(BaseModel, related_name='functions')
    expression = ExpressionOneToOneAttribute(FunctionExpression, related_name='function')
    units = StringAttribute()

    class Meta(Model.Meta, ExpressionExpressionTermMeta):
        expression_term_model = FunctionExpression


class ExpressionAttributesTestCase(unittest.TestCase):
    def test_one_to_one_serialize(self):
        expr = 'p_1 + p_2'
        p_1 = Parameter(id='p_1', value=2., units='dimensionless')
        p_2 = Parameter(id='p_2', value=3., units='dimensionless')
        expression, error = FunctionExpression.deserialize(expr, {
            Parameter: {p_1.id: p_1, p_2.id: p_2},
        })
        assert error is None, str(error)
        self.assertEqual(Function.expression.serialize(expression), expr)
        self.assertEqual(Function.expression.serialize(''), '')
        self.assertEqual(Function.expression.serialize(None), '')

    def test_one_to_one_deserialize(self):
        expr = 'p_1 + p_2'
        p_1 = Parameter(id='p_1', value=2., units='dimensionless')
        p_2 = Parameter(id='p_2', value=3., units='dimensionless')

        expression, error = Function.expression.deserialize(expr, {
            Parameter: {p_1.id: p_1, p_2.id: p_2},
        })
        assert error is None, str(error)
        self.assertEqual(Function.expression.serialize(expression), expr)

        self.assertEqual(Function.expression.deserialize('', {}), (None, None))

    def test_many_to_one_serialize(self):
        expr = 'p_1 + p_2'
        p_1 = Parameter(id='p_1', value=2., units='dimensionless')
        p_2 = Parameter(id='p_2', value=3., units='dimensionless')
        expression, error = LinearSubFunctionExpression.deserialize(expr, {
            Parameter: {p_1.id: p_1, p_2.id: p_2},
        })
        assert error is None, str(error)
        self.assertEqual(LinearSubFunction.expression.serialize(expression), expr)
        self.assertEqual(LinearSubFunction.expression.serialize(''), '')
        self.assertEqual(LinearSubFunction.expression.serialize(None), '')

    def test_many_to_one_deserialize(self):
        expr = 'p_1 + p_2'
        p_1 = Parameter(id='p_1', value=2., units='dimensionless')
        p_2 = Parameter(id='p_2', value=3., units='dimensionless')

        expression, error = LinearSubFunction.expression.deserialize(expr, {
            Parameter: {p_1.id: p_1, p_2.id: p_2},
        })
        assert error is None, str(error)
        self.assertEqual(LinearSubFunction.expression.serialize(expression), expr)

        self.assertEqual(LinearSubFunction.expression.deserialize('', {}), (None, None))


class ExpressionTestCase(unittest.TestCase):
    def test_deserialize_repeated(self):
        expr_1 = FunctionExpression()
        objects = {FunctionExpression: {'1': expr_1}}

        expr_2, error = FunctionExpression.deserialize('1', objects)
        self.assertEqual(error, None)

        self.assertEqual(expr_2, expr_1)

    def test_deserialize_is_linear(self):
        objects = {
            Parameter: {
                'p_1': Parameter(id='p_1'),
                'p_2': Parameter(id='p_2'),
                'p_3': Parameter(id='p_3'),
            },
        }

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + p_3', objects)
        self.assertTrue(expr._parsed_expression.is_linear)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 - p_3', objects)
        self.assertTrue(expr._parsed_expression.is_linear)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + 2 * p_3', objects)
        self.assertTrue(expr._parsed_expression.is_linear)

        expr, error = FunctionExpression.deserialize('p_1 * p_2 + p_3', objects)
        self.assertFalse(expr._parsed_expression.is_linear)

    def test_deserialize_error(self):
        rv = FunctionExpression.deserialize('1 * ', {})
        self.assertEqual(rv[0], None)
        self.assertRegex(str(rv[1]), 'SyntaxError:')

        rv = Expression.deserialize(FunctionExpression, '1 * ', {})
        self.assertEqual(rv[0], None)
        self.assertRegex(str(rv[1]), 'SyntaxError:')

        rv = Expression.deserialize(Function, '1 * ', {})
        self.assertEqual(rv[0], None)
        self.assertRegex(str(rv[1]), "doesn't have a 'Meta.expression_term_models' attribute")

    def test_validate(self):
        objects = {
            Parameter: {
                'p_1': Parameter(id='p_1'),
                'p_2': Parameter(id='p_2'),
                'p_3': Parameter(id='p_3'),
            },
        }

        expr, error = FunctionExpression.deserialize('p_1 + p_2+ p_3', objects)
        self.assertEqual(expr.validate(), None)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + p_3', objects)
        expr.expression = 'p_1 * p_4'
        self.assertNotEqual(expr.validate(), None)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + p_3', objects)
        expr.expression = 'p_1 + p_2'
        self.assertNotEqual(expr.validate(), None)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 * p_3', objects)
        self.assertEqual(expr.validate(), None)
        expr, error = LinearSubFunctionExpression.deserialize('p_1 + p_2 * p_3', objects)
        self.assertNotEqual(expr.validate(), None)

        expr, error = FunctionExpression.deserialize('p_1 > p_2', objects)
        self.assertNotEqual(expr.validate(), None)
        expr, error = SubFunctionExpression.deserialize('p_1 > p_2', objects)
        self.assertEqual(expr.validate(), None)

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + p_3', objects)
        expr.expression = '1['
        rv = Expression.validate(expr, None)
        self.assertRegex(str(rv), 'Python syntax error')

        expr, error = FunctionExpression.deserialize('p_1 + p_2 + p_3', objects)
        expr.expression = 'p_1 + p_2 + p_3 + 1 / 0'
        rv = Expression.validate(expr, None)
        self.assertRegex(str(rv), 'cannot eval expression')

    def test_make_expression_obj(self):
        objects = {
            Parameter: {
                'p_1': Parameter(id='p_1'),
                'p_2': Parameter(id='p_2'),
                'p_3': Parameter(id='p_3'),
            },
        }
        rv = Expression.make_expression_obj(Function, 'p_1 + p_2 + p_3', objects)
        self.assertIsInstance(rv[0], FunctionExpression)
        self.assertEqual(rv[1], None)

    def test_make_obj(self):
        objects = {
            Parameter: {
                'p_1': Parameter(id='p_1'),
                'p_2': Parameter(id='p_2'),
                'p_3': Parameter(id='p_3'),
            },
        }

        func_1 = Expression.make_obj(BaseModel(), Function, 'func_1', 'p_1 + p_2 + p_3', objects)
        self.assertIsInstance(func_1, Function)
        self.assertEqual(func_1.id, 'func_1')
        self.assertEqual(func_1.expression.expression, 'p_1 + p_2 + p_3')

        self.assertIsInstance(Expression.make_obj(BaseModel(), Function, 'func_1', 'p_1 + p_2 + ', objects), InvalidAttribute)

        self.assertIsInstance(Expression.make_obj(BaseModel(), LinearSubFunction, 'func_1',
                                                  'p_1 * p_2 + p_3', objects), InvalidObject)


class ParsedExpressionTestCase(unittest.TestCase):
    @staticmethod
    def esc_re_center(re_list):
        return '.*' + '.*'.join([re.escape(an_re) for an_re in re_list]) + '.*'

    def test___init__(self):
        expr = '3 + 5 * 6'
        parsed_expr = ParsedExpression(SubFunctionExpression, 'attr', ' ' + expr + ' ', {})
        self.assertEqual(expr, parsed_expr.expression)

        with self.assertRaisesRegex(ParsedExpressionError, 'is not a subclass of Model'):
            ParsedExpression(int, 'attr', expr, {})

        with self.assertRaisesRegex(ParsedExpressionError, "doesn't have a 'Meta.expression_term_models' attribute"):
            ParsedExpression(Model, 'attr', expr, {})

        with self.assertRaisesRegex(ParsedExpressionError, "creates a Python syntax error"):
            ParsedExpression(SubFunctionExpression, 'attr', '3(', {})

        class TestModelExpression(Model):
            class Meta(Model.Meta):
                expression_term_models = ('Function',)
        with self.assertRaisesRegex(ParsedExpressionError, 'must have a relationship to'):
            ParsedExpression(TestModelExpression, 'attr', expr, {})

    def test_parsed_expression(self):
        expr = '3 + 5 * 6'
        parsed_expr = ParsedExpression(FunctionExpression, 'attr', ' ' + expr + ' ', {})
        self.assertEqual(expr, parsed_expr.expression)
        n = 5
        parsed_expr = ParsedExpression(FunctionExpression, 'attr', ' + ' * n, {})
        self.assertEqual([token.PLUS] * n, [tok.exact_type for tok in parsed_expr._py_tokens])
        parsed_expr = ParsedExpression(FunctionExpression, 'attr', '', {})
        self.assertEqual(parsed_expr.valid_functions, set(FunctionExpression.Meta.expression_valid_functions))
        parsed_expr = ParsedExpression(FunctionExpression, 'attr', '', {Function: {}, Parameter: {}})
        self.assertEqual(parsed_expr.valid_functions, set(FunctionExpression.Meta.expression_valid_functions))
        expr = 'id1[id2'
        with self.assertRaisesRegex(
                ParsedExpressionError,
                "parsing '{}'.*creates a Python syntax error.*".format(re.escape(expr))):
            self.make_parsed_expr(expr)
        with self.assertRaisesRegex(
                ParsedExpressionError,
                "model_cls 'Species' doesn't have a 'Meta.expression_term_models' attribute"):
            ParsedExpression(Species, 'attr', '', {})

    def test_parsed_expression_ambiguous(self):
        func, error = FunctionExpression.deserialize('min(p_1, p_2)', {
            Parameter: {
                'min': Parameter(id='min', value=1.),
                'p_1': Parameter(id='p_1', value=2.),
                'p_2': Parameter(id='p_2', value=3.),
            }
        })
        self.assertEqual(func, None)
        self.assertRegex(str(error), 'is ambiguous. ObjModelToken matches')

    def test__get_model_type(self):
        expr = '3 + 5 * 6'
        parsed_expr = ParsedExpression(SubFunctionExpression, 'attr', expr, {})
        self.assertEqual(parsed_expr._get_model_type('SubFunction'), SubFunction)
        self.assertEqual(parsed_expr._get_model_type('NoSuchType'), None)

    def make_parsed_expr(self, expr, obj_type=FunctionExpression, objects=None):
        objects = objects or {}
        return ParsedExpression(obj_type, 'expr_attr', expr, objects)

    def do_match_tokens_test(self, expr, pattern, expected, idx=0):
        parsed_expr = self.make_parsed_expr(expr)
        self.assertEqual(parsed_expr._match_tokens(pattern, idx), expected)

    def test_match_tokens(self):
        self.do_match_tokens_test('', [], False)
        single_name_pattern = (token.NAME, )
        self.do_match_tokens_test('', single_name_pattern, False)
        self.do_match_tokens_test('ID2', single_name_pattern, 'ID2')
        self.do_match_tokens_test('ID3 5', single_name_pattern, 'ID3')
        # fail to match tokens
        self.do_match_tokens_test('+ 5', single_name_pattern, False)
        # call _match_tokens with 0<idx
        self.do_match_tokens_test('7 ID3', single_name_pattern, 'ID3', idx=1)
        self.do_match_tokens_test('2+ 5', single_name_pattern, False, idx=1)

        pattern = (token.NAME, token.LSQB, token.NAME, token.RSQB)
        self.do_match_tokens_test('sp1[c1]+', pattern, 'sp1[c1]')
        self.do_match_tokens_test('sp1 +', pattern, False)
        # whitespace is not allowed between tokens in an ID
        self.do_match_tokens_test('sp1 [ c1 ] ', pattern, False)

    def do_disambiguated_id_error_test(self, expr, expected):
        parsed_expr = self.make_parsed_expr(expr)
        result = parsed_expr._get_disambiguated_id(0)
        self.assertTrue(isinstance(result, str))
        self.assertIn(expected.format(expr), result)

    def do_disambiguated_id_test(self, expr, disambig_type, id, pattern, case_fold_match=False, objects=None):
        parsed_expr = self.make_parsed_expr(expr, objects=objects)
        lex_match = parsed_expr._get_disambiguated_id(0, case_fold_match=case_fold_match)
        self.assertIsInstance(lex_match, LexMatch)
        self.assertEqual(lex_match.num_py_tokens, len(pattern))
        self.assertEqual(len(lex_match.obj_model_tokens), 1)
        obj_model_token = lex_match.obj_model_tokens[0]
        self.assertEqual(obj_model_token,
                         # note: obj_model_token.model is cheating
                         ObjModelToken(ObjModelTokenCodes.obj_id, expr, disambig_type,
                                       id, obj_model_token.model))

    def test_disambiguated_id(self):
        self.do_disambiguated_id_error_test(
            'Parameter.foo2',
            "contains '{}', but 'foo2' is not the id of a 'Parameter'")

        self.do_disambiguated_id_error_test(
            'NotFunction.foo',
            "contains '{}', but the disambiguation model type 'NotFunction' cannot be referenced by ")
        self.do_disambiguated_id_error_test(
            'NoSuchModel.fun_1',
            "contains '{}', but the disambiguation model type 'NoSuchModel' cannot be referenced by "
            "'FunctionExpression' expressions")
        self.do_disambiguated_id_error_test(
            'Parameter.fun_1',
            "contains '{}', but 'fun_1' is not the id of a 'Parameter'")

        objects = {
            SubFunction: {'test_id': SubFunction(id='test_id')}
        }
        self.do_disambiguated_id_test('SubFunction.test_id', SubFunction, 'test_id',
                                      ParsedExpression.MODEL_TYPE_DISAMBIG_PATTERN, objects=objects)
        self.do_disambiguated_id_test('SubFunction.TEST_ID', SubFunction, 'test_id',
                                      ParsedExpression.MODEL_TYPE_DISAMBIG_PATTERN, objects=objects, case_fold_match=True)

        # do not find a match
        parsed_expr = self.make_parsed_expr('3 * 2')
        self.assertEqual(parsed_expr._get_disambiguated_id(0), None)

    def do_related_object_id_error_test(self, expr, expected_error, objects):
        parsed_expr = self.make_parsed_expr(expr, objects=objects)
        result = parsed_expr._get_related_obj_id(0)
        self.assertIsInstance(result, str)
        self.assertRegex(result, self.esc_re_center(expected_error))

    def test_related_object_id_errors(self):
        objects = {}
        self.do_related_object_id_error_test(
            'x[c]',
            ["contains the identifier(s)", "which aren't the id(s) of an object"],
            objects)

    def test_related_object_id_mult_matches_error(self):
        objects = {
            SubFunction: {'test_id': SubFunction()},
            LinearSubFunction: {'test_id': LinearSubFunction()},
        }
        self.do_related_object_id_error_test(
            'test_id',
            ["multiple model object id matches: 'test_id' as a LinearSubFunction id, 'test_id' as a SubFunction id"],
            objects)

    def do_related_object_id_test(self, expr, expected_token_string, expected_related_type,
                                  expected_id, pattern, case_fold_match=False, objects=None):
        parsed_expr = self.make_parsed_expr(expr, objects=objects)
        lex_match = parsed_expr._get_related_obj_id(0, case_fold_match=case_fold_match)
        self.assertIsInstance(lex_match, LexMatch)
        self.assertEqual(lex_match.num_py_tokens, len(pattern))
        self.assertEqual(len(lex_match.obj_model_tokens), 1)
        obj_model_token = lex_match.obj_model_tokens[0]

        self.assertEqual(obj_model_token,
                         # note: obj_model_token.model is cheating
                         ObjModelToken(ObjModelTokenCodes.obj_id, expected_token_string,
                                       expected_related_type,
                                       expected_id, obj_model_token.model))

    def test_related_object_id_matches(self):
        objects = {
            Parameter: {'test_id': Parameter(id='test_id')},
            SubFunction: {'sub_func': SubFunction(id='sub_func')},
        }
        self.do_related_object_id_test('test_id + 3*x', 'test_id', Parameter, 'test_id',
                                       Parameter.Meta.expression_term_token_pattern, objects=objects)
        self.do_related_object_id_test('sub_func', 'sub_func', SubFunction, 'sub_func', (token.NAME, ), objects=objects)
        self.do_related_object_id_test('sub_Func', 'sub_Func', SubFunction, 'sub_func', (token.NAME, ),
                                       objects=objects, case_fold_match=True)
        self.do_related_object_id_test('SUB_FUNC', 'SUB_FUNC', SubFunction, 'sub_func', (token.NAME, ),
                                       objects=objects, case_fold_match=True)

        # no token matches
        parsed_expr = self.make_parsed_expr("3 * 4")
        self.assertEqual(parsed_expr._get_related_obj_id(0), None)

    def do_fun_call_error_test(self, expr, expected_error, obj_type=FunctionExpression):
        parsed_expr = self.make_parsed_expr(expr, obj_type=obj_type)
        result = parsed_expr._get_func_call_id(0)
        self.assertTrue(isinstance(result, str))
        self.assertRegex(result, self.esc_re_center(expected_error))

    def test_fun_call_id_errors(self):
        self.do_fun_call_error_test('foo(3)', ["contains the func name ",
                                               "but it isn't in {}.Meta.expression_valid_functions".format(
                                                   FunctionExpression.__name__)])

        class TestModelExpression(Model):
            functions = ManyToManyAttribute(Function, related_name='test_model_expressions')

            class Meta(Model.Meta):
                expression_term_models = ('Function',)
        self.do_fun_call_error_test('foo(3)', ["contains the func name ",
                                               "but {}.Meta doesn't define 'expression_valid_functions'".format(
                                                   TestModelExpression.__name__)],
                                    obj_type=TestModelExpression)

    def test_fun_call_id(self):
        parsed_expr = self.make_parsed_expr('log(3)')
        lex_match = parsed_expr._get_func_call_id(0)
        self.assertTrue(isinstance(lex_match, LexMatch))
        self.assertEqual(lex_match.num_py_tokens, len(parsed_expr.FUNC_PATTERN))
        self.assertEqual(len(lex_match.obj_model_tokens), 2)
        self.assertEqual(lex_match.obj_model_tokens[0], ObjModelToken(ObjModelTokenCodes.math_func_id, 'log'))
        self.assertEqual(lex_match.obj_model_tokens[1], ObjModelToken(ObjModelTokenCodes.op, '('))

        # no token match
        parsed_expr = self.make_parsed_expr('no_fun + 3')
        self.assertEqual(parsed_expr._get_func_call_id(0), None)

    def test_bad_tokens(self):
        rv, _, errors = ParsedExpression(FunctionExpression, 'test', '+= *= @= : {}', {}).tokenize()
        self.assertEqual(rv, None)
        for bad_tok in ['+=', '*=', '@=', ':', '{', '}']:
            self.assertRegex(errors[0], r'.*contains bad token\(s\):.*' + re.escape(bad_tok) + '.*')
        # test bad tokens that don't have string values
        rv, _, errors = ParsedExpression(FunctionExpression, 'test', """
 3
 +1""", {}).tokenize()
        self.assertEqual(rv, None)
        self.assertRegex(errors[0], re.escape("contains bad token(s)"))

    def do_tokenize_id_test(self, expr, expected_wc_tokens, expected_related_objs,
                            model_type=FunctionExpression,
                            test_objects=None, case_fold_match=False):
        if test_objects is None:
            test_objects = {
                Parameter: {
                    'test_id': Parameter(),
                    'x_id': Parameter(),
                },
                SubFunction: {
                    'Observable': LinearSubFunction(),
                    'duped_id': LinearSubFunction(),
                },
                LinearSubFunction: {
                    'test_id': SubFunction(),
                    'duped_id': SubFunction(),
                },
            }
        parsed_expr = ParsedExpression(model_type, 'attr', expr, test_objects)
        obj_model_tokens, related_objects, _ = parsed_expr.tokenize(case_fold_match=case_fold_match)
        self.assertEqual(parsed_expr.errors, [])
        self.assertEqual(obj_model_tokens, expected_wc_tokens)
        for obj_types in test_objects:
            if obj_types in expected_related_objs.keys():
                self.assertEqual(related_objects[obj_types], expected_related_objs[obj_types])
            else:
                self.assertEqual(related_objects[obj_types], {})

    def extract_from_objects(self, objects, type_id_pairs):
        d = {}
        for obj_type, id in type_id_pairs:
            if obj_type not in d:
                d[obj_type] = {}
            d[obj_type][id] = objects[obj_type][id]
        return d

    def test_non_identifier_tokens(self):
        expr = ' 7 * ( 5 - 3 ) / 2'
        expected_wc_tokens = [
            ObjModelToken(code=ObjModelTokenCodes.number, token_string='7'),
            ObjModelToken(code=ObjModelTokenCodes.op, token_string='*'),
            ObjModelToken(code=ObjModelTokenCodes.op, token_string='('),
            ObjModelToken(code=ObjModelTokenCodes.number, token_string='5'),
            ObjModelToken(code=ObjModelTokenCodes.op, token_string='-'),
            ObjModelToken(code=ObjModelTokenCodes.number, token_string='3'),
            ObjModelToken(code=ObjModelTokenCodes.op, token_string=')'),
            ObjModelToken(code=ObjModelTokenCodes.op, token_string='/'),
            ObjModelToken(code=ObjModelTokenCodes.number, token_string='2'),
        ]
        self.do_tokenize_id_test(expr, expected_wc_tokens, {})

    def test_tokenize_w_ids(self):
        # test _get_related_obj_id
        expr = 'test_id'
        sub_func = SubFunction(id=expr)
        objs = {
            SubFunction: {
                expr: sub_func,
                'duped_id': SubFunction(),
            },
            Parameter: {
                'duped_id': Parameter(),
            },
        }
        expected_wc_tokens = \
            [ObjModelToken(ObjModelTokenCodes.obj_id, expr, SubFunction,
                           expr, sub_func)]
        expected_related_objs = self.extract_from_objects(objs, [(SubFunction, expr)])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs, test_objects=objs)

        # test _get_disambiguated_id
        expr = 'Parameter.duped_id + 2*SubFunction.duped_id'
        expected_wc_tokens = [
            ObjModelToken(ObjModelTokenCodes.obj_id, 'Parameter.duped_id', Parameter, 'duped_id',
                          objs[Parameter]['duped_id']),
            ObjModelToken(ObjModelTokenCodes.op, '+'),
            ObjModelToken(ObjModelTokenCodes.number, '2'),
            ObjModelToken(ObjModelTokenCodes.op, '*'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'SubFunction.duped_id', SubFunction, 'duped_id',
                          objs[SubFunction]['duped_id']),
        ]
        expected_related_objs = self.extract_from_objects(objs, [(Parameter, 'duped_id'),
                                                                 (SubFunction, 'duped_id')])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs, test_objects=objs)

        # test _get_func_call_id
        expr = 'log(3) + func_1 - SubFunction.Function'
        objs = {SubFunction: {'func_1': SubFunction(), 'Function': SubFunction()}}
        expected_wc_tokens = [
            ObjModelToken(code=ObjModelTokenCodes.math_func_id, token_string='log'),
            ObjModelToken(ObjModelTokenCodes.op, '('),
            ObjModelToken(ObjModelTokenCodes.number, '3'),
            ObjModelToken(ObjModelTokenCodes.op, ')'),
            ObjModelToken(ObjModelTokenCodes.op, '+'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'func_1', SubFunction, 'func_1',
                          objs[SubFunction]['func_1']),
            ObjModelToken(ObjModelTokenCodes.op, '-'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'SubFunction.Function', SubFunction, 'Function',
                          objs[SubFunction]['Function'])
        ]
        expected_related_objs = self.extract_from_objects(objs,
                                                          [(SubFunction, 'func_1'), (SubFunction, 'Function')])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs, test_objects=objs)

        # test case_fold_match=True for _get_related_obj_id and _get_disambiguated_id
        expr = 'TEST_ID - SubFunction.DUPED_ID'
        objs = {
            SubFunction: {'test_id': SubFunction(), 'duped_id': SubFunction()},
            LinearSubFunction: {'duped_id': LinearSubFunction()},
        }
        expected_wc_tokens = [
            ObjModelToken(ObjModelTokenCodes.obj_id, 'TEST_ID', SubFunction, 'test_id',
                          objs[SubFunction]['test_id']),
            ObjModelToken(ObjModelTokenCodes.op, '-'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'SubFunction.DUPED_ID', SubFunction, 'duped_id',
                          objs[SubFunction]['duped_id']),
        ]
        expected_related_objs = self.extract_from_objects(objs, [(SubFunction, 'duped_id'),
                                                                 (SubFunction, 'test_id')])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs, case_fold_match=True, test_objects=objs)

    def test_tokenize_w_multiple_ids(self):
        # at idx==0 match more than one of these _get_related_obj_id(), _get_disambiguated_id(), _get_func_call_id()
        # test _get_related_obj_id and _get_disambiguated_id'
        test_objects = {
            LinearSubFunction: {'SubFunction': SubFunction()},
            SubFunction: {'test_id': LinearSubFunction()}
        }
        expr = 'SubFunction.test_id'
        expected_wc_tokens = [
            ObjModelToken(ObjModelTokenCodes.obj_id, expr, SubFunction, 'test_id',
                          test_objects[SubFunction]['test_id'])
        ]
        expected_related_objs = self.extract_from_objects(test_objects, [(SubFunction, 'test_id')])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs,
                                 test_objects=test_objects)

        # test _get_related_obj_id and _get_func_call_id'
        test_objects = {
            SubFunction: {'Function': Parameter()},
            LinearSubFunction: {'fun_2': Function()}
        }
        expr = 'LinearSubFunction.fun_2'
        expected_wc_tokens = [
            ObjModelToken(ObjModelTokenCodes.obj_id, expr, LinearSubFunction, 'fun_2',
                          test_objects[LinearSubFunction]['fun_2'])
        ]
        expected_related_objs = self.extract_from_objects(test_objects, [(LinearSubFunction, 'fun_2')])
        self.do_tokenize_id_test(expr, expected_wc_tokens, expected_related_objs,
                                 test_objects=test_objects)

    def do_tokenize_error_test(self, expr, expected_errors, model_type=FunctionExpression, test_objects=None):
        if test_objects is None:
            test_objects = {
                SubFunction: {'SubFunction': SubFunction()},
                LinearSubFunction: {'SubFunction': LinearSubFunction()},
            }
        parsed_expr = ParsedExpression(model_type, 'attr', expr, test_objects)
        sb_none, _, errors = parsed_expr.tokenize()
        self.assertEqual(sb_none, None)
        # expected_errors is a list of lists of strings that should match the actual errors
        expected_errors = [self.esc_re_center(ee) for ee in expected_errors]
        self.assertEqual(len(errors), len(expected_errors),
                         "Counts differ: num errors {} != Num expected errors {}".format(
            len(errors), len(expected_errors)))
        print(errors)
        expected_errors_found = {}
        for expected_error in expected_errors:
            expected_errors_found[expected_error] = False
        for error in errors:
            for expected_error in expected_errors:
                if re.match(expected_error, error):
                    if expected_errors_found[expected_error]:
                        self.fail("Expected error '{}' matches again".format(expected_error))
                    expected_errors_found[expected_error] = True
        for expected_error, status in expected_errors_found.items():
            self.assertTrue(status, "Expected error '{}' not found in errors".format(expected_error))

    def test_tokenize_errors(self):
        bad_id = 'no_such_id'
        self.do_tokenize_error_test(
            bad_id,
            [["contains the identifier(s) '{}', which aren't the id(s) of an object".format(bad_id)]])
        bad_id = 'SubFunction.no_such_observable'
        self.do_tokenize_error_test(
            bad_id,
            [["contains multiple model object id matches: 'SubFunction' as a LinearSubFunction id, 'SubFunction' as a SubFunction id"],
             ["contains '{}', but '{}'".format(bad_id, bad_id.split('.')[1]), "is not the id of a"]])
        bad_id = 'no_such_function'
        bad_fn_name = bad_id
        self.do_tokenize_error_test(
            bad_fn_name,
            [["contains the identifier(s) '{}', which aren't the id(s) of an object".format(bad_id)]])
        bad_id = 'LinearSubFunction'
        bad_fn_name = bad_id+'.no_such_function2'
        self.do_tokenize_error_test(
            bad_fn_name,
            [["contains the identifier(s) '{}', which aren't the id(s) of an object".format(bad_id)],
             ["contains '{}', but '{}'".format(bad_fn_name, bad_fn_name.split('.')[1]), "is not the id of a"]])

        expr, error = FunctionExpression.deserialize('p_1 + p_2', {
            Parameter: {'p_1': Parameter(id='p_1'), 'p_2': Parameter(id='p_2'), }
        })
        assert error is None, str(error)
        expr._parsed_expression.expression = ''
        rv = expr._parsed_expression.tokenize()
        self.assertEqual(rv[0], None)
        self.assertEqual(rv[1], None)
        self.assertIn('Expression cannot be empty', rv[2])

    def test_str(self):
        expr = 'func_1 + LinearSubFunction.func_2'
        parsed_expr = self.make_parsed_expr(expr, objects={
            SubFunction: {'func_1': SubFunction()},
            LinearSubFunction: {'func_2': LinearSubFunction()},
        })
        self.assertIn(expr, str(parsed_expr))
        self.assertIn('errors: []', str(parsed_expr))
        self.assertIn('obj_model_tokens: []', str(parsed_expr))
        parsed_expr.tokenize()
        self.assertIn(expr, str(parsed_expr))
        self.assertIn('errors: []', str(parsed_expr))
        self.assertIn('obj_model_tokens: [ObjModelToken', str(parsed_expr))

    def test_model_class_lacks_meta(self):
        class Foo(object):
            pass
        objects = {
            Foo: {'foo_1': Foo(), 'foo_2': Foo()}
        }
        with self.assertRaisesRegex(ParsedExpressionError,
                                    "model_cls 'Foo' is not a subclass of Model"):
            ParsedExpression(Foo, 'expr_attr', '', objects)

    def do_test_eval(self, expr, parent_type, obj_type, related_obj_val, expected_val):
        objects = {
            Parameter: {
                'p_1': Parameter(id='p_1', value=1.),
                'p_2': Parameter(id='p_2', value=2.),
            },
            Species: {
                's_1[c_1]': Species(id='s_1[c_1]'),
                's_2[c_2]': Species(id='s_2[c_2]'),
            },
            SubFunction: {
                'func_1': SubFunction(id='func_1'),
            },
        }
        objects[SubFunction]['func_1'].expression, error = SubFunctionExpression.deserialize('2 * p_2', objects)
        assert error is None, str(error)

        obj, error = Expression.deserialize(obj_type, expr, objects)
        assert error is None, str(error)
        parsed_expr = obj._parsed_expression
        parent = parent_type(expression=obj)
        evaled_val = parsed_expr.test_eval({Species: related_obj_val})
        self.assertEqual(expected_val, evaled_val)

    def test_parsed_expression_compile_error(self):
        expr = '3 + 5 * 6'
        parsed_expr = ParsedExpression(FunctionExpression, 'attr', expr, {})
        parsed_expr.tokenize()
        self.assertEqual(parsed_expr.errors, [])

        parsed_expr._compile()

        parsed_expr._obj_model_tokens = None
        with self.assertRaisesRegex(ParsedExpressionError, 'not been successfully tokenized'):
            parsed_expr._compile()

    def test_test_eval(self):
        self.do_test_eval('p_1', Function, FunctionExpression, 1., 1.)
        self.do_test_eval('3 * p_1', Function, FunctionExpression, 1., 3.)
        self.do_test_eval('3 * p_2', Function, FunctionExpression, 1., 6.)
        self.do_test_eval('s_1[c_1]', Function, FunctionExpression, 1., 1.)
        self.do_test_eval('s_1[c_1]', Function, FunctionExpression, 2., 2.)
        self.do_test_eval('2 * s_1[c_1]', Function, FunctionExpression, 2., 4.)
        self.do_test_eval('func_1', Function, FunctionExpression, 1., 4.)
        self.do_test_eval('p_1 + func_1', Function, FunctionExpression, 1., 5.)

        # test combination of ObjModelTokenCodes
        expected_val = 4 * 1. + pow(2, 2.) + 4.
        self.do_test_eval('4 * p_1 + pow(2, p_2) + func_1', Function, FunctionExpression,
                          None, expected_val)

        # test different model classes
        expected_val = 4 * 1. + pow(2, 2.)
        self.do_test_eval('4 * p_1 + pow(2, p_2)', SubFunction, SubFunctionExpression,
                          None, expected_val)

        # test different exceptions
        # syntax error
        model_type = FunctionExpression
        parsed_expr = self.make_parsed_expr('4 *', obj_type=model_type)
        rv = parsed_expr.tokenize()
        self.assertEqual(rv[0], None)
        self.assertEqual(rv[1], None)
        self.assertRegex(str(rv[2]), 'SyntaxError')

        # expression that could not be serialized
        expr = 'foo(6)'
        parsed_expr = self.make_parsed_expr(expr, obj_type=model_type)
        parsed_expr.tokenize()
        model = model_type(expression=parsed_expr)
        with self.assertRaisesRegex(ParsedExpressionError,
                                    re.escape("Cannot evaluate '{}', as it not been "
                                              "successfully compiled".format(expr))):
            parsed_expr.test_eval()

    def test_eval_with_units(self):
        func = Function(id='func', units='g l^-1')
        func.expression, error = FunctionExpression.deserialize('p_1 / p_2', {
            Parameter: {
                'p_1': Parameter(id='p_1', value=2., units='g'),
                'p_2': Parameter(id='p_2', value=5., units='l'),
            }
        })
        assert error is None, str(error)

        rv = func.expression._parsed_expression.eval({}, with_units=True)
        self.assertEqual(rv.magnitude, 0.4)
        self.assertEqual(rv.to_base_units().units, unit_registry.parse_expression('g l^-1').to_base_units().units)

        class Units(Enum):
            g = 1
            l = 2
        func.expression.parameters.get_one(id='p_1').units = Units.g
        func.expression.parameters.get_one(id='p_2').units = Units.l
        rv = func.expression._parsed_expression.eval({}, with_units=True)
        self.assertEqual(rv.magnitude, 0.4)
        self.assertEqual(rv.to_base_units().units, unit_registry.parse_expression('g l^-1').to_base_units().units)

    def test_eval_with_units_and_boolean(self):
        func_1 = SubFunction(id='func_1', units='dimensionless')
        func_1.expression, error = SubFunctionExpression.deserialize('p_1 < p_2', {
            Parameter: {
                'p_1': Parameter(id='p_1', value=2., units='g'),
                'p_2': Parameter(id='p_2', value=5., units='l'),
            }
        })
        assert error is None, str(error)

        func_2 = Function(id='func_2', units='g l^-1')
        func_2.expression, error = FunctionExpression.deserialize('(p_3 / p_4) * func_1', {
            Parameter: {
                'p_3': Parameter(id='p_3', value=2., units='g'),
                'p_4': Parameter(id='p_4', value=5., units='l'),
            },
            SubFunction: {
                func_1.id: func_1,
            },
        })
        assert error is None, str(error)

        rv = func_2.expression._parsed_expression.eval({}, with_units=True)
        self.assertEqual(rv.magnitude, 0.4)
        self.assertEqual(rv.to_base_units().units, unit_registry.parse_expression('g l^-1').to_base_units().units)

        func_1.expression.parameters.get_one(id='p_1').value = 10.
        rv = func_2.expression._parsed_expression.eval({}, with_units=True)
        self.assertEqual(rv.magnitude, 0.)
        self.assertEqual(rv.to_base_units().units, unit_registry.parse_expression('g l^-1').to_base_units().units)

    def test_eval_error(self):
        func = Function(id='func', units='g l^-1')
        func.expression, error = FunctionExpression.deserialize('p_1 / p_2', {
            Parameter: {
                'p_1': Parameter(id='p_1', value=2., units='g'),
                'p_2': Parameter(id='p_2', value=5., units='l'),
            }
        })
        assert error is None, str(error)

        func.expression._parsed_expression._compiled_expression = '1 *'
        with self.assertRaisesRegex(ParsedExpressionError, 'SyntaxError'):
            func.expression._parsed_expression.eval({})

        func.expression._parsed_expression._compiled_expression = 'p_3'
        with self.assertRaisesRegex(ParsedExpressionError, 'NameError'):
            func.expression._parsed_expression.eval({})

        func.expression._parsed_expression._compiled_expression = '1 / 0'
        with self.assertRaisesRegex(ParsedExpressionError, 'Exception'):
            func.expression._parsed_expression.eval({})


class ParsedExpressionErrorTestCase(unittest.TestCase):
    def test___init__(self):
        exception = ParsedExpressionError('test message')
        self.assertEqual(exception.args, ('test message',))


class ParsedExpressionValidatorTestCase(unittest.TestCase):

    def test_expression_verifier(self):

        number_is_good_transitions = [   # (current state, message, next state)
            ('start', (ObjModelTokenCodes.number, None), 'accept'),
        ]
        expression_verifier = ParsedExpressionValidator('start', 'accept', number_is_good_transitions)
        number_is_good = [
            ObjModelToken(ObjModelTokenCodes.number, '3'),
        ]
        valid, error = expression_verifier.validate(mock.Mock(_obj_model_tokens=number_is_good))
        self.assertTrue(valid)
        self.assertTrue(error is None)
        # an empty expression is invalid
        valid, error = expression_verifier.validate(mock.Mock(_obj_model_tokens=[]))
        self.assertFalse(valid)

    def test_linear_expression_verifier(self):

        obj_model_tokens = [   # id0 - 3*id1 - 3.5*id1 + 3.14e+2*id3
            ObjModelToken(ObjModelTokenCodes.obj_id, 'id0'),
            ObjModelToken(ObjModelTokenCodes.op, '-'),
            ObjModelToken(ObjModelTokenCodes.number, '3'),
            ObjModelToken(ObjModelTokenCodes.op, '*'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'id1'),
            ObjModelToken(ObjModelTokenCodes.op, '-'),
            ObjModelToken(ObjModelTokenCodes.number, '3.5'),
            ObjModelToken(ObjModelTokenCodes.op, '*'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'id1'),
            ObjModelToken(ObjModelTokenCodes.op, '+'),
            ObjModelToken(ObjModelTokenCodes.number, '3.14e+2'),
            ObjModelToken(ObjModelTokenCodes.op, '*'),
            ObjModelToken(ObjModelTokenCodes.obj_id, 'id3'),
        ]
        valid_linear_expr = mock.Mock(_obj_model_tokens=obj_model_tokens)

        linear_expression_verifier = LinearParsedExpressionValidator()
        valid, error = linear_expression_verifier.validate(valid_linear_expr)
        self.assertTrue(valid)
        self.assertTrue(error is None)
        # dropping any single token from obj_model_tokens produces an invalid expression
        for i in range(len(obj_model_tokens)):
            wc_tokens_without_i = obj_model_tokens[:i] + obj_model_tokens[i+1:]
            valid, error = linear_expression_verifier.validate(mock.Mock(_obj_model_tokens=wc_tokens_without_i))
            self.assertFalse(valid)

        # an empty expression is valid
        valid, error = linear_expression_verifier.validate(mock.Mock(_obj_model_tokens=[]))
        self.assertTrue(valid)
        self.assertTrue(error is None)

        invalid_wc_tokens = [
            [ObjModelToken(ObjModelTokenCodes.math_func_id, 'log')],     # math functions not allowed
            [ObjModelToken(ObjModelTokenCodes.number, '3j')],           # numbers must be floats
        ]
        for invalid_wc_token in invalid_wc_tokens:
            valid, error = linear_expression_verifier.validate(mock.Mock(_obj_model_tokens=invalid_wc_token))
            self.assertFalse(valid)

        invalid_wc_tokens = [
            [ObjModelToken(ObjModelTokenCodes.other, ',')],             # other not allowed
        ]
        for invalid_wc_token in invalid_wc_tokens:
            error = linear_expression_verifier._make_dfsa_messages(invalid_wc_token)
            self.assertTrue(error is None)
