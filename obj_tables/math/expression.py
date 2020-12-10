""" Utilities for processing mathematical expressions used by obj_tables models

:Author: Arthur Goldberg <Arthur.Goldberg@mssm.edu>
:Author: Jonathan Karr <karr@mssm.edu>
:Date: 2018-12-19
:Copyright: 2016-2019, Karr Lab
:License: MIT
"""

from enum import Enum
from io import BytesIO
import ast
import astor
import collections
import copy
import keyword
import math
import pint  # noqa: F401
import re
import token
import tokenize
import types  # noqa: F401
from obj_tables.core import (Model, RelatedAttribute, OneToOneAttribute, ManyToOneAttribute,
                             InvalidObject, InvalidAttribute)
from wc_utils.util.misc import DFSMAcceptor

__all__ = [
    'OneToOneExpressionAttribute',
    'ManyToOneExpressionAttribute',
    'ObjTablesTokenCodes',
    'IdMatch',
    'ObjTablesToken',
    'LexMatch',
    'ExpressionTermMeta',
    'ExpressionStaticTermMeta',
    'ExpressionDynamicTermMeta',
    'ExpressionExpressionTermMeta',
    'Expression',
    'ParsedExpressionError',
    'ParsedExpression',
    'LinearParsedExpressionValidator',
]


class ObjTablesTokenCodes(int, Enum):
    """ ObjTablesToken codes used in parsed expressions """
    obj_id = 1
    math_func_id = 2
    number = 3
    op = 4
    other = 5


# a matched token pattern used by tokenize
IdMatch = collections.namedtuple('IdMatch', 'model_type, token_pattern, match_string')
IdMatch.__doc__ += ': Matched token pattern used by tokenize'
IdMatch.model_type.__doc__ = 'The type of Model matched'
IdMatch.token_pattern.__doc__ = 'The token pattern used by the match'
IdMatch.match_string.__doc__ = 'The matched string'


# a token in a parsed expression, returned in a list by tokenize
ObjTablesToken = collections.namedtuple('ObjTablesToken', 'code, token_string, model_type, model_id, model')
# make model_type, model_id, and model optional: see https://stackoverflow.com/a/18348004
ObjTablesToken.__new__.__defaults__ = (None, None, None)
ObjTablesToken.__doc__ += ': ObjTablesToken in a parsed obj_tables expression'
ObjTablesToken.code.__doc__ = 'ObjTablesTokenCodes encoding'
ObjTablesToken.token_string.__doc__ = "The token's string"
ObjTablesToken.model_type.__doc__ = "When code is obj_id, the obj_tables obj's type"
ObjTablesToken.model_id.__doc__ = "When code is obj_id, the obj_tables obj's id"
ObjTablesToken.model.__doc__ = "When code is obj_id, the obj_tables obj"


# container for an unambiguous Model id
LexMatch = collections.namedtuple('LexMatch', 'obj_tables_tokens, num_py_tokens')
LexMatch.__doc__ += ': container for an unambiguous Model id'
LexMatch.obj_tables_tokens.__doc__ = "List of ObjTablesToken's created"
LexMatch.num_py_tokens.__doc__ = 'Number of Python tokens consumed'


class OneToOneExpressionAttribute(OneToOneAttribute):
    """ Expression one-to-one attribute """

    def serialize(self, expression, encoded=None):
        """ Serialize related object

        Args:
            expression (:obj:`obj_tables.Model`): the referenced :obj:`Expression`
            encoded (:obj:`dict`, optional): dictionary of objects that have already been encoded

        Returns:
            :obj:`str`: simple Python representation
        """
        if expression:
            return expression.serialize()
        else:
            return ''

    def deserialize(self, value, objects, decoded=None):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model
            decoded (:obj:`dict`, optional): dictionary of objects that have already been decoded

        Returns:
            :obj:`tuple` of :obj:`object`, :obj:`InvalidAttribute` or :obj:`None`: tuple of cleaned value and cleaning error
        """
        if value:
            return self.related_class.deserialize(value, objects)
        return (None, None)

    def get_xlsx_validation(self, sheet_models=None, doc_metadata_model=None):
        """ Get XLSX validation

        Args:
            sheet_models (:obj:`list` of :obj:`Model`, optional): models encoded as separate sheets
            doc_metadata_model (:obj:`type`): model whose worksheet contains the document metadata

        Returns:
            :obj:`wc_utils.workbook.io.FieldValidation`: validation
        """
        validation = super(OneToOneAttribute, self).get_xlsx_validation(sheet_models=sheet_models,
                                                                        doc_metadata_model=doc_metadata_model)

        if self.related_class.Meta.expression_is_linear:
            type = 'linear '
        else:
            type = ''

        terms = []
        for attr in self.related_class.Meta.attributes.values():
            if isinstance(attr, RelatedAttribute) and \
                    attr.related_class.__name__ in self.related_class.Meta.expression_term_models:
                terms.append(attr.related_class.Meta.verbose_name_plural)
        if terms:
            if len(terms) == 1:
                terms = terms[0]
            else:
                terms = '{} and {}'.format(', '.join(terms[0:-1]), terms[-1])

            input_message = 'Enter a {}expression of {}.'.format(type, terms)
            error_message = 'Value must be a {}expression of {}.'.format(type, terms)
        else:
            input_message = 'Enter a {}expression.'.format(type, terms)
            error_message = 'Value must be a {}expression.'.format(type, terms)

        if validation.input_message:
            validation.input_message += '\n\n'
        validation.input_message += input_message

        if validation.error_message:
            validation.error_message += '\n\n'
        validation.error_message += error_message

        return validation


class ManyToOneExpressionAttribute(ManyToOneAttribute):
    """ Expression many-to-one attribute """

    def serialize(self, expression, encoded=None):
        """ Serialize related object

        Args:
            expression (:obj:`Expression`): the related :obj:`Expression`
            encoded (:obj:`dict`, optional): dictionary of objects that have already been encoded

        Returns:
            :obj:`str`: simple Python representation of the rate law expression
        """
        if expression:
            return expression.serialize()
        else:
            return ''

    def deserialize(self, value, objects, decoded=None):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model
            decoded (:obj:`dict`, optional): dictionary of objects that have already been decoded

        Returns:
            :obj:`tuple` of :obj:`object`, :obj:`InvalidAttribute` or :obj:`None`: tuple of cleaned value and cleaning error
        """
        if value:
            return self.related_class.deserialize(value, objects)
        return (None, None)

    def get_xlsx_validation(self, sheet_models=None, doc_metadata_model=None):
        """ Get XLSX validation

        Args:
            sheet_models (:obj:`list` of :obj:`Model`, optional): models encoded as separate sheets
            doc_metadata_model (:obj:`type`): model whose worksheet contains the document metadata

        Returns:
            :obj:`wc_utils.workbook.io.FieldValidation`: validation
        """
        validation = super(ManyToOneAttribute, self).get_xlsx_validation(sheet_models=sheet_models,
                                                                         doc_metadata_model=doc_metadata_model)

        if self.related_class.Meta.expression_is_linear:
            type = 'linear '
        else:
            type = ''

        terms = []
        for attr in self.related_class.Meta.attributes.values():
            if isinstance(attr, RelatedAttribute) and \
                    attr.related_class.__name__ in self.related_class.Meta.expression_term_models:
                terms.append(attr.related_class.Meta.verbose_name_plural)
        if terms:
            if len(terms) == 1:
                terms = terms[0]
            else:
                terms = '{} and {}'.format(', '.join(terms[0:-1]), terms[-1])

            input_message = 'Enter a {}expression of {}.'.format(type, terms)
            error_message = 'Value must be a {}expression of {}.'.format(type, terms)
        else:
            input_message = 'Enter a {}expression.'.format(type, terms)
            error_message = 'Value must be a {}expression.'.format(type, terms)

        if validation.input_message:
            validation.input_message += '\n\n'
        validation.input_message += input_message

        if validation.error_message:
            validation.error_message += '\n\n'
        validation.error_message += error_message

        return validation


class ExpressionTermMeta(object):
    """ Metadata for subclasses that can appear in expressions

    Attributes:
        expression_term_token_pattern (:obj:`tuple`): token pattern for the name of the
            term in expression
        expression_term_units (:obj:`str`): name of attribute which describes the units
            of the expression term
    """
    expression_term_token_pattern = (token.NAME, )
    expression_term_units = 'units'


class ExpressionStaticTermMeta(ExpressionTermMeta):
    """ Metadata for subclasses with static values that can appear in expressions

    Attributes:
        expression_term_value (:obj:`str`): name of attribute which encodes the value of
            the term
    """
    expression_term_value = 'value'


class ExpressionDynamicTermMeta(ExpressionTermMeta):
    """ Metadata for subclasses with dynamic values that can appear in expressions """
    pass


class ExpressionExpressionTermMeta(ExpressionTermMeta):
    """ Metadata for subclasses with expressions that can appear in expressions

    Attributes:
        expression_term_model (:obj:`str`): name of attribute which encodes the expression for
            the term
    """
    expression_term_model = None


class Expression(object):
    """ Generic methods for mathematical expressions

    Attributes:
        _parsed_expression (:obj:`ParsedExpression`): parsed expression
    """

    class Meta(object):
        """ Metadata for subclasses of :obj:`Expression`

        Attributes:
            expression_term_models (:obj:`tuple` of :obj:`str`): names of classes
                which can appear as terms in the expression
            expression_valid_functions (:obj:`tuple` of :obj:`types.FunctionType`): Python
                functions which can appear in the expression
            expression_is_linear (:obj:`bool`): if :obj:`True`, validate that the expression is linear
            expression_type (:obj:`type`): type of the expression
            expression_unit_registry (:obj:`pint.UnitRegistry`): unit registry
        """
        expression_term_models = ()
        expression_valid_functions = (
            float,

            math.fabs,
            math.ceil,
            math.floor,
            round,

            math.exp,
            math.expm1,
            math.pow,
            math.sqrt,
            math.log,
            math.log1p,
            math.log10,
            math.log2,

            math.factorial,

            math.sin,
            math.cos,
            math.tan,
            math.acos,
            math.asin,
            math.atan,
            math.atan2,
            math.hypot,

            math.degrees,
            math.radians,

            min,
            max)
        expression_is_linear = False
        expression_type = None
        expression_unit_registry = None

    def serialize(self):
        """ Generate string representation

        Returns:
            :obj:`str`: value of primary attribute
        """
        return self.expression

    @classmethod
    def deserialize(cls, model_cls, value, objects):
        """ Deserialize :obj:`value` into an :obj:`Expression`

        Args:
            model_cls (:obj:`type`): :obj:`Expression` class or subclass
            value (:obj:`str`): string representation of the mathematical expression, in a
                Python expression
            objects (:obj:`dict`): dictionary of objects which can be used in :obj:`Expression`, grouped by model

        Returns:
            :obj:`tuple`: on error return (:obj:`None`, :obj:`InvalidAttribute`),
                otherwise return (object in this class with instantiated :obj:`_parsed_expression`, :obj:`None`)
        """
        value = value or ''

        expr_field = 'expression'
        try:
            parsed_expression = ParsedExpression(model_cls, expr_field, value, objects)
        except ParsedExpressionError as e:
            attr = model_cls.Meta.attributes['expression']
            return (None, InvalidAttribute(attr, [str(e)]))
        _, used_objects, errors = parsed_expression.tokenize()
        if errors:
            attr = model_cls.Meta.attributes['expression']
            return (None, InvalidAttribute(attr, errors))
        if model_cls not in objects:
            objects[model_cls] = {}
        if value in objects[model_cls]:
            obj = objects[model_cls][value]
        else:
            obj = model_cls(expression=value)
            objects[model_cls][value] = obj

            for attr_name, attr in model_cls.Meta.attributes.items():
                if isinstance(attr, RelatedAttribute) and \
                        attr.related_class.__name__ in model_cls.Meta.expression_term_models:
                    attr_value = list(used_objects.get(attr.related_class, {}).values())
                    setattr(obj, attr_name, attr_value)
        obj._parsed_expression = parsed_expression

        # check expression is linear and, if so, compute linear coefficients for the related objects
        parsed_expression.is_linear, _ = LinearParsedExpressionValidator().validate(parsed_expression)

        return (obj, None)

    @classmethod
    def validate(cls, model_obj, parent_obj):
        """ Determine whether an expression model is valid

        One check eval's its deserialized expression

        Args:
            model_obj (:obj:`Expression`): expression object
            parent_obj (:obj:`Model`): parent of expression object

        Returns:
            :obj:`InvalidObject` or None: :obj:`None` if the object is valid,
                otherwise return a list of errors in an :obj:`InvalidObject` instance
        """
        model_cls = model_obj.__class__

        # generate _parsed_expression
        objs = {}
        for related_attr_name, related_attr in model_cls.Meta.attributes.items():
            if isinstance(related_attr, RelatedAttribute):
                objs[related_attr.related_class] = {
                    m.get_primary_attribute(): m for m in getattr(model_obj, related_attr_name)
                }
        try:
            model_obj._parsed_expression = ParsedExpression(model_obj.__class__, 'expression',
                                                            model_obj.expression, objs)
        except ParsedExpressionError as e:
            attr = model_cls.Meta.attributes['expression']
            attr_err = InvalidAttribute(attr, [str(e)])
            return InvalidObject(model_obj, [attr_err])

        is_valid, _, errors = model_obj._parsed_expression.tokenize()
        if is_valid is None:
            attr = model_cls.Meta.attributes['expression']
            attr_err = InvalidAttribute(attr, errors)
            return InvalidObject(model_obj, [attr_err])
        model_obj._parsed_expression.is_linear, _ = LinearParsedExpressionValidator().validate(
            model_obj._parsed_expression)

        # check that related objects match the tokens of the _parsed_expression
        related_objs = {}
        for related_attr_name, related_attr in model_cls.Meta.attributes.items():
            if isinstance(related_attr, RelatedAttribute):
                related_model_objs = getattr(model_obj, related_attr_name)
                if related_model_objs:
                    related_objs[related_attr.related_class] = set(related_model_objs)

        token_objs = {}
        token_obj_ids = {}
        for obj_table_token in model_obj._parsed_expression._obj_tables_tokens:
            if obj_table_token.model_type is not None:
                if obj_table_token.model_type not in token_objs:
                    token_objs[obj_table_token.model_type] = set()
                    token_obj_ids[obj_table_token.model_type] = set()
                token_objs[obj_table_token.model_type].add(obj_table_token.model)
                token_obj_ids[obj_table_token.model_type].add(obj_table_token.token_string)

        if related_objs != token_objs:
            attr = model_cls.Meta.attributes['expression']
            attr_err = InvalidAttribute(attr, ['Related objects must match the tokens of the analyzed expression'])
            return InvalidObject(model_obj, [attr_err])

        # check that expression is valid
        try:
            rv = model_obj._parsed_expression.test_eval()
            if model_obj.Meta.expression_type:
                if not isinstance(rv, model_obj.Meta.expression_type):
                    attr = model_cls.Meta.attributes['expression']
                    attr_err = InvalidAttribute(attr,
                                                ["Evaluating '{}', a {} expression, should return a {} but it returns a {}".format(
                                                    model_obj.expression, model_obj.__class__.__name__,
                                                    model_obj.Meta.expression_type.__name__, type(rv).__name__)])
                    return InvalidObject(model_obj, [attr_err])
        except ParsedExpressionError as e:
            attr = model_cls.Meta.attributes['expression']
            attr_err = InvalidAttribute(attr, [str(e)])
            return InvalidObject(model_obj, [attr_err])

        # check expression is linear
        if model_obj.Meta.expression_is_linear and not model_obj._parsed_expression.is_linear:
            attr = model_cls.Meta.attributes['expression']
            attr_err = InvalidAttribute(attr, ['Expression must be linear in species counts'])
            return InvalidObject(model_obj, [attr_err])

        # return :obj:`None` to indicate valid object
        return None

    @staticmethod
    def make_expression_obj(model_type, expression, objs):
        """ Make an expression object

        Args:
            model_type (:obj:`type`): an :obj:`Model` that uses a mathemetical expression, like
                :obj:`Function` and :obj:`Observable`
            expression (:obj:`str`): the expression used by the :obj:`model_type` being created
            objs (:obj:`dict` of :obj:`dict`): all objects that are referenced in :obj:`expression`

        Returns:
            :obj:`tuple`: if successful, (:obj:`Model`, :obj:`None`) containing a new instance of
                :obj:`model_type`'s expression helper class; otherwise, (:obj:`None`, :obj:`InvalidAttribute`)
                reporting the error
        """
        expr_model_type = model_type.Meta.expression_term_model
        return expr_model_type.deserialize(expression, objs)

    @classmethod
    def make_obj(cls, model, model_type, primary_attr, expression, objs, allow_invalid_objects=False):
        """ Make a model that contains an expression by using its expression helper class

        For example, this uses :obj:`FunctionExpression` to make a :obj:`Function`.

        Args:
            model (:obj:`Model`): an instance of :obj:`Model` which is the root model
            model_type (:obj:`type`): a subclass of :obj:`Model` that uses a mathemetical expression, like
                :obj:`Function` and :obj:`Observable`
            primary_attr (:obj:`object`): the primary attribute of the :obj:`model_type` being created
            expression (:obj:`str`): the expression used by the :obj:`model_type` being created
            objs (:obj:`dict` of :obj:`dict`): all objects that are referenced in :obj:`expression`
            allow_invalid_objects (:obj:`bool`, optional): if set, return object - not error - if
                the expression object does not validate

        Returns:
            :obj:`Model` or :obj:`InvalidAttribute`: a new instance of :obj:`model_type`, or,
                if an error occurs, an :obj:`InvalidAttribute` reporting the error
        """
        expr_model_obj, error = cls.make_expression_obj(model_type, expression, objs)
        if error:
            return error
        error_or_none = expr_model_obj.validate()
        if error_or_none is not None and not allow_invalid_objects:
            return error_or_none
        related_name = model_type.Meta.attributes['model'].related_name
        related_in_model = getattr(model, related_name)
        new_obj = related_in_model.create(expression=expr_model_obj)
        setattr(new_obj, model_type.Meta.primary_attribute.name, primary_attr)
        return new_obj

    def merge_attrs(self, other, other_objs_in_self, self_objs_in_other):
        """ Merge attributes of two objects

        Args:
            other (:obj:`Model`): other model
            other_objs_in_self (:obj:`dict`): dictionary that maps instances of objects in another model to objects
                in a model
            self_objs_in_other (:obj:`dict`): dictionary that maps instances of objects in a model to objects
                in another model
        """
        for cls, other_related_objs in other._parsed_expression.related_objects.items():
            for obj_id, other_obj in other_related_objs.items():
                self._parsed_expression.related_objects[cls][obj_id] = other_objs_in_self.get(other_obj, other_obj)


class ParsedExpressionError(ValueError):
    """ Exception raised for errors in :obj:`ParsedExpression`

    Attributes:
        message (:obj:`str`): the exception's message
    """

    def __init__(self, message=None):
        """
        Args:
            message (:obj:`str`, optional): the exception's message
        """
        super().__init__(message)


class ParsedExpression(object):
    """ An expression in an :obj:`ObjTables` :obj:`Model`

    These expressions are limited Python expressions with specific semantics:

    * They must be syntactically correct Python, except that an identifier can begin with numerical digits.
    * No Python keywords, strings, or tokens that do not belong in expressions are allowed.
    * All Python identifiers must be the primary attribute of an :obj:`ObjTables` object or the name of a
      function in the :obj:`math` package. Objects in the model
      are provided in :obj:`_objs`, and the allowed subset of functions in :obj:`math` must be provided in an
      iterator in the :obj:`expression_valid_functions` attribute of the :obj:`Meta` class of a model whose whose expression
      is being processed.
    * Currently (July, 2018), an identifier may refer to a :obj:`Species`, :obj:`Parameter`,
      :obj:`Reaction`, :obj:`Observable` or :obj:`DfbaObjReaction`.
    * Cycles of references are illegal.
    * An identifier must unambiguously refer to exactly one related :obj:`Model` in a model.
    * Each :obj:`Model` that can be used in an expression must have an ID that is an identifier,
      or define :obj:`expression_term_token_pattern` as an attribute that describes the :obj:`Model`\ 's
      syntactic Python structure. See :obj:`Species` for an example.
    * Every expression must be computable at any time during a simulation. The evaluation of an expression
      always occurs at a precise simulation time, which is implied by the expression but not explicitly
      represented. E.g., a reference to a :obj:`Species` means its concentration at the time the expression is
      :obj:`eval`\ ed. These are the meanings of references:

      * :obj:`Species`: its current concentration
      * :obj:`Parameter`: its value, which is static
      * :obj:`Observable`: its current value, whose units depend on its definition
      * :obj:`Reaction`: its current flux
      * :obj:`DfbaObjReaction`: its current flux

    The modeller is responsible for ensuring that units in expressions are internally consistent and appropriate
    for the expression's use.

    Attributes:
        model_cls (:obj:`type`): the :obj:`Model` which has an expression
        attr (:obj:`str`): the attribute name of the expression in :obj:`model_cls`
        expression (:obj:`str`): the expression defined in the obj_tables :obj:`Model`
        _py_tokens (:obj:`list` of :obj:`collections.namedtuple`): a list of Python tokens generated by :obj:`tokenize.tokenize()`
        _objs (:obj:`dict`): dict of obj_tables Models that might be referenced in :obj:`expression`;
            maps model type to a dict mapping ids to Model instances
        valid_functions (:obj:`set`): the union of all :obj:`valid_functions` attributes for :obj:`_objs`
        unit_registry (:obj:`pint.UnitRegistry`): unit registry
        related_objects (:obj:`dict`): models that are referenced in :obj:`expression`; maps model type to
            dict that maps model id to model instance
        lin_coeffs (:obj:`dict`): linear coefficients of models that are referenced in :obj:`expression`;
            maps model type to dict that maps models to coefficients
        errors (:obj:`list` of :obj:`str`): errors found when parsing an :obj:`expression` fails
        _obj_tables_tokens (:obj:`list` of :obj:`ObjTablesToken`): tokens obtained when an :obj:`expression`
            is successfully :obj:`tokenize`\ d; if empty, then this :obj:`ParsedExpression` cannot use :obj:`eval`
        _compiled_expression (:obj:`str`): compiled expression that can be evaluated by :obj:`eval`
        _compiled_expression_with_units (:obj:`str`): compiled expression with units that can be evaluated by :obj:`eval`
        _compiled_namespace (:obj:`dict`): compiled namespace for evaluation by :obj:`eval`
        _compiled_namespace_with_units (:obj:`dict`): compiled namespace with units for evaluation by :obj:`eval`
    """

    # ModelType.model_id
    MODEL_TYPE_DISAMBIG_PATTERN = (token.NAME, token.DOT, token.NAME)
    FUNC_PATTERN = (token.NAME, token.LPAR)

    # enumerate and detect Python tokens that are legal in obj_tables expressions
    LEGAL_TOKENS_NAMES = (
        'NUMBER',  # number
        'NAME',  # variable names
        'LSQB', 'RSQB',  # for compartment names
        'DOT',  # for disambiguating variable types
        'COMMA',  # for function arguments
        'DOUBLESTAR', 'MINUS', 'PLUS', 'SLASH', 'STAR',  # mathematical operators
        'LPAR', 'RPAR',  # for mathematical grouping and functions
        'EQEQUAL', 'GREATER', 'GREATEREQUAL', 'LESS', 'LESSEQUAL', 'NOTEQUAL',  # comparison operators
    )
    LEGAL_TOKENS = set()
    for legal_token_name in LEGAL_TOKENS_NAMES:
        legal_token = getattr(token, legal_token_name)
        LEGAL_TOKENS.add(legal_token)

    def __init__(self, model_cls, attr, expression, objs):
        """ Create an instance of ParsedExpression

        Args:
            model_cls (:obj:`type`): the :obj:`Model` which has an expression
            attr (:obj:`str`): the attribute name of the expression in :obj:`model_cls`
            expression (:obj:`obj`): the expression defined in the obj_tables :obj:`Model`
            objs (:obj:`dict`): dictionary of model objects (instances of :obj:`Model`) organized
                by their type

        Raises:
            :obj:`ParsedExpressionError`: if :obj:`model_cls` is not a subclass of :obj:`Model`,
                or lexical analysis of :obj:`expression` raises an exception,
                or :obj:`objs` includes model types that :obj:`model_cls` should not reference
        """
        if not issubclass(model_cls, Model):
            raise ParsedExpressionError("model_cls '{}' is not a subclass of Model".format(
                model_cls.__name__))
        if not hasattr(model_cls.Meta, 'expression_term_models'):
            raise ParsedExpressionError("model_cls '{}' doesn't have a 'Meta.expression_term_models' attribute".format(
                model_cls.__name__))
        self.term_models = set()
        for expression_term_model_type_name in model_cls.Meta.expression_term_models:
            related_class = None
            for attr in model_cls.Meta.attributes.values():
                if isinstance(attr, RelatedAttribute) \
                        and attr.related_class.__name__ == expression_term_model_type_name:
                    related_class = attr.related_class
                    break
            if related_class:
                self.term_models.add(related_class)
            else:
                raise ParsedExpressionError('Expression term {} must have a relationship to {}'.format(
                    expression_term_model_type_name, model_cls.__name__))
        self.valid_functions = set()
        if hasattr(model_cls.Meta, 'expression_valid_functions'):
            self.valid_functions.update(model_cls.Meta.expression_valid_functions)

        self.unit_registry = model_cls.Meta.expression_unit_registry

        self._objs = objs
        self.model_cls = model_cls
        self.attr = attr
        if isinstance(expression, int) or isinstance(expression, float):
            expression = str(expression)
        if not isinstance(expression, str):
            raise ParsedExpressionError(f"Expression '{expression}' in {model_cls.__name__} must be "
                                        "string, float or integer")
        # strip leading and trailing whitespace from expression, which would create a bad token error
        self.expression = expression.strip()

        # allow identifiers that start with a number
        expr = self.__prep_expr_for_tokenization(self.expression)

        try:
            g = tokenize.tokenize(BytesIO(expr.encode('utf-8')).readline)
            # strip the leading ENCODING token and trailing NEWLINE and ENDMARKER tokens
            self._py_tokens = list(g)[1:-1]
            if self._py_tokens and self._py_tokens[-1].type == token.NEWLINE:
                self._py_tokens = self._py_tokens[:-1]
        except tokenize.TokenError as e:
            raise ParsedExpressionError("parsing '{}', a {}.{}, creates a Python syntax error: '{}'".format(
                self.expression, self.model_cls.__name__, self.attr, str(e)))

        self.__reset_tokenization()

    @staticmethod
    def __prep_expr_for_tokenization(expr):
        """ Prepare an expression for tokenization with the Python tokenizer

        * Add prefix ("__digit__") to names (identifiers of obj_tables objects) that begin with a number

        Args:
            expr (:obj:`str`): expression

        Returns:
            :obj:`str`: prepared expression
        """
        return re.sub(r'(^|\b)'
                      # ignore tokens which are regular, exponential, and hexidecimal numbers
                      r'(?!((0[x][0-9a-f]+(\b|$))|([0-9]+e[\-\+]?[0-9]+(\b|$))))'
                      r'([0-9]+[a-z_][0-9a-z_]*)'
                      r'(\b|$)',
                      r'__digit__\7', expr, flags=re.I)

    def __reset_tokenization(self):
        """ Reset tokenization
        """
        self.related_objects = {}
        self.lin_coeffs = {}
        for model_type in self.term_models:
            self.related_objects[model_type] = {}
            self.lin_coeffs[model_type] = {}

        self.errors = []
        self._obj_tables_tokens = []
        self._compiled_expression = ''
        self._compiled_expression_with_units = ''
        self._compiled_namespace = {}
        self._compiled_namespace_with_units = {}

    def _get_trailing_whitespace(self, idx):
        """ Get the number of trailing spaces following a Python token

        Args:
            idx (:obj:`int`): index of the token in :obj:`self._py_tokens`
        """
        if len(self._py_tokens) - 1 <= idx:
            return 0
        # get distance between the next token's start column and end column of the token at idx
        # assumes that an expression uses only one line
        return self._py_tokens[idx + 1].start[1] - self._py_tokens[idx].end[1]

    def recreate_whitespace(self, expr):
        """ Insert the whitespace in this object's :obj:`expression` into an expression with the same token count

        Used to migrate an expression to a different set of model type names.

        Args:
            expr (:obj:`str`): a syntactically correct Python expression

        Returns:
            :obj:`str`: :obj:`expr` with the whitespace in this instance's :obj:`expression` inserted between
                its Python tokens

        Raises:
            :obj:`ParsedExpressionError`: if tokenizing :obj:`expr` raises an exception,
                or if :obj:`expr` doesn't have the same number of Python tokens as :obj:`self.expression`
        """
        prepped_expr = self.__prep_expr_for_tokenization(expr)
        try:
            g = tokenize.tokenize(BytesIO(prepped_expr.encode('utf-8')).readline)
            # strip the leading ENCODING marker and trailing NEWLINE and ENDMARKER tokens
            tokens = list(g)[1:-1]
            if tokens and tokens[-1].type == token.NEWLINE:
                tokens = tokens[:-1]
        except tokenize.TokenError as e:
            raise ParsedExpressionError("parsing '{}' creates a Python syntax error: '{}'".format(
                expr, str(e)))
        if len(tokens) != len(self._py_tokens):
            raise ParsedExpressionError("can't recreate whitespace in '{}', as it has {} instead "
                                        "of {} tokens expected".format(expr, len(tokens), len(self._py_tokens)))

        expanded_expr = []
        for i_tok, tok in enumerate(tokens):
            if tok.type == token.NAME and tok.string.startswith('__digit__'):
                expanded_expr.append(tok.string[9:])
            else:
                expanded_expr.append(tok.string)
            ws = ' ' * self._get_trailing_whitespace(i_tok)
            expanded_expr.append(ws)
        return ''.join(expanded_expr)

    def _get_model_type(self, name):
        """ Find the `ObjTables` model type corresponding to :obj:`name`

        Args:
            name (:obj:`str`): the name of a purported `ObjTables` model type in an expression

        Returns:
            :obj:`object`: :obj:`None` if no model named :obj:`name` exists in :obj:`self.term_models`,
                else the type of the model with that name
        """
        for model_type in self.term_models:
            if name == model_type.__name__:
                return model_type
        return None

    def _match_tokens(self, token_pattern, idx):
        """ Indicate whether :obj:`tokens` begins with a pattern of tokens that match :obj:`token_pattern`

        Args:
            token_pattern (:obj:`tuple` of :obj:`int`): a tuple of Python token numbers, taken from the
            :obj:`token` module
            idx (:obj:`int`): current index into :obj:`tokens`

        Returns:
            :obj:`object`: :obj:`bool`, False if the initial elements of :obj:`tokens` do not match the
            syntax in :obj:`token_pattern`, or :obj:`str`, the matching string
        """
        if not token_pattern:
            return False
        if len(self._py_tokens) - idx < len(token_pattern):
            return False
        for tok_idx, token_pat_num in enumerate(token_pattern):
            if self._py_tokens[idx + tok_idx].exact_type != token_pat_num:
                return False
            # because a obj_tables primary attribute shouldn't contain white space, do not allow it between the self._py_tokens
            # that match token_pattern
            if 0 < tok_idx and self._py_tokens[idx + tok_idx - 1].end != self._py_tokens[idx + tok_idx].start:
                return False

        match_val = ''
        for tok in self._py_tokens[idx:idx + len(token_pattern)]:
            if tok.type == token.NAME and tok.string.startswith('__digit__'):
                match_val += tok.string[9:]
            else:
                match_val += tok.string
        return match_val

    def _get_disambiguated_id(self, idx, case_fold_match=False):
        """ Try to parse a disambiguated `ObjTables` id from :obj:`self._py_tokens` at :obj:`idx`

        Look for a disambugated id (a Model written as :obj:`ModelType.model_id`). If tokens do not match,
        return :obj:`None`. If tokens match, but their values are wrong, return an error :obj:`str`.
        If a disambugated id is found, return a :obj:`LexMatch` describing it.

        Args:
            idx (:obj:`int`): current index into :obj:`tokens`
            case_fold_match (:obj:`bool`, optional): if set, :obj:`casefold()` identifiers before matching;
                in a :obj:`ObjTablesToken`, :obj:`token_string` retains the original expression text, while :obj:`model_id`
                contains the casefold'ed value; identifier keys in :obj:`self._objs` must already be casefold'ed;
                default=False

        Returns:
            :obj:`object`: If tokens do not match, return :obj:`None`. If tokens match,
                but their values are wrong, return an error :obj:`str`.
                If a disambugated id is found, return a :obj:`LexMatch` describing it.
        """
        disambig_model_match = self._match_tokens(self.MODEL_TYPE_DISAMBIG_PATTERN, idx)
        if disambig_model_match:
            disambig_model_type = self._py_tokens[idx].string
            possible_model_id = self._py_tokens[idx + 2].string
            if case_fold_match:
                possible_model_id = possible_model_id.casefold()

            # the disambiguation model type must be in self.term_models
            model_type = self._get_model_type(disambig_model_type)
            if model_type is None:
                return ("'{}', a {}.{}, contains '{}', but the disambiguation model type '{}' "
                        "cannot be referenced by '{}' expressions".format(
                            self.expression, self.model_cls.__name__,
                            self.attr, disambig_model_match, disambig_model_type,
                            self.model_cls.__name__))

            if possible_model_id not in self._objs.get(model_type, {}):
                return "'{}', a {}.{}, contains '{}', but '{}' is not the id of a '{}'".format(
                    self.expression, self.model_cls.__name__, self.attr, disambig_model_match,
                    possible_model_id, disambig_model_type)

            return LexMatch([ObjTablesToken(ObjTablesTokenCodes.obj_id, disambig_model_match, model_type,
                                            possible_model_id, self._objs[model_type][possible_model_id])],
                            len(self.MODEL_TYPE_DISAMBIG_PATTERN))

        # no match
        return None

    def _get_related_obj_id(self, idx, case_fold_match=False):
        """ Try to parse a related object `ObjTables` id from :obj:`self._py_tokens` at :obj:`idx`

        Different `ObjTables` objects match different Python token patterns. The default pattern
        is (token.NAME, ), but an object of type :obj:`model_type` can define a custom pattern in
        :obj:`model_type.Meta.expression_term_token_pattern`, as :obj:`Species` does. Some patterns may consume
            multiple Python tokens.

        Args:
            idx (:obj:`int`): current index into :obj:`_py_tokens`
            case_fold_match (:obj:`bool`, optional): if set, casefold identifiers before matching;
                identifier keys in :obj:`self._objs` must already be casefold'ed; default=False

        Returns:
            :obj:`object`: If tokens do not match, return :obj:`None`. If tokens match,
                but their values are wrong, return an error :obj:`str`.
                If a related object id is found, return a :obj:`LexMatch` describing it.
        """
        token_matches = set()
        id_matches = set()
        for model_type in self.term_models:
            token_pattern = model_type.Meta.expression_term_token_pattern
            match_string = self._match_tokens(token_pattern, idx)
            if match_string:
                token_matches.add(match_string)
                # is match_string the ID of an instance in model_type?
                if case_fold_match:
                    if match_string.casefold() in self._objs.get(model_type, {}):
                        id_matches.add(IdMatch(model_type, token_pattern, match_string))
                else:
                    if match_string in self._objs.get(model_type, {}):
                        id_matches.add(IdMatch(model_type, token_pattern, match_string))

        if not id_matches:
            if token_matches:
                return ("'{}', a {}.{}, contains the identifier(s) '{}', which aren't "
                        "the id(s) of an object".format(
                            self.expression, self.model_cls.__name__,
                            self.attr, "', '".join(token_matches)))
            return None

        if 1 < len(id_matches):
            # as lexers always do, pick the longest match
            id_matches_by_length = sorted(id_matches, key=lambda id_match: len(id_match.match_string))
            longest_length = len(id_matches_by_length[-1].match_string)
            longest_matches = set()
            while id_matches_by_length and len(id_matches_by_length[-1].match_string) == longest_length:
                longest_matches.add(id_matches_by_length.pop())
            id_matches = longest_matches

        if 1 < len(id_matches):
            # error: multiple, maximal length matches
            matches_error = ["'{}' as a {} id".format(id_val, model_type.__name__)
                             for model_type, _, id_val in sorted(id_matches, key=lambda id_match: id_match.model_type.__name__)]
            matches_error = ', '.join(matches_error)
            return "'{}', a {}.{}, contains multiple model object id matches: {}".format(
                self.expression, self.model_cls.__name__, self.attr, matches_error)

        else:
            # return a lexical match about a related id
            match = id_matches.pop()
            right_case_match_string = match.match_string
            if case_fold_match:
                right_case_match_string = match.match_string.casefold()
            return LexMatch(
                [ObjTablesToken(ObjTablesTokenCodes.obj_id, match.match_string, match.model_type, right_case_match_string,
                                self._objs[match.model_type][right_case_match_string])],
                len(match.token_pattern))

    def _get_func_call_id(self, idx, case_fold_match='unused'):
        """ Try to parse a Python math function call from :obj:`self._py_tokens` at :obj:`idx`

        Each `ObjTables` object :obj:`model_cls` that contains an expression which can use Python math
        functions must define the set of allowed functions in :obj:`Meta.expression_valid_functions` of the
        model_cls Expression Model.

        Args:
            idx (:obj:`int`): current index into :obj:`self._py_tokens`
            case_fold_match (:obj:`str`, optional): ignored keyword; makes :obj:`ParsedExpression.tokenize()` simpler

        Returns:
            :obj:`object`: If tokens do not match, return :obj:`None`. If tokens match,
                but their values are wrong, return an error :obj:`str`.
                If a function call is found, return a :obj:`LexMatch` describing it.
        """
        func_match = self._match_tokens(self.FUNC_PATTERN, idx)
        if func_match:
            func_name = self._py_tokens[idx].string
            # FUNC_PATTERN is "identifier ("
            # the closing paren ")" will simply be encoded as a ObjTablesToken with code == op

            # are Python math functions defined?
            if not hasattr(self.model_cls.Meta, 'expression_valid_functions'):
                return ("'{}', a {}.{}, contains the func name '{}', but {}.Meta doesn't "
                        "define 'expression_valid_functions'".format(self.expression,
                                                                     self.model_cls.__name__,
                                                                     self.attr, func_name,
                                                                     self.model_cls.__name__))

            function_ids = set([f.__name__ for f in self.model_cls.Meta.expression_valid_functions])

            # is the function allowed?
            if func_name not in function_ids:
                return ("'{}', a {}.{}, contains the func name '{}', but it isn't in "
                        "{}.Meta.expression_valid_functions: {}".format(self.expression,
                                                                        self.model_cls.__name__,
                                                                        self.attr, func_name,
                                                                        self.model_cls.__name__,
                                                                        ', '.join(function_ids)))

            # return a lexical match about a math function
            return LexMatch(
                [ObjTablesToken(ObjTablesTokenCodes.math_func_id, func_name), ObjTablesToken(ObjTablesTokenCodes.op, '(')],
                len(self.FUNC_PATTERN))

        # no match
        return None

    def tokenize(self, case_fold_match=False):
        """ Tokenize a Python expression in :obj:`self.expression`

        Args:
            case_fold_match (:obj:`bool`, optional): if set, casefold identifiers before matching;
                identifier keys in :obj:`self._objs` must already be casefold'ed; default = False

        Returns:
            :obj:`tuple`:

                * :obj:`list`: of :obj:`ObjTablesToken`\ s
                * :obj:`dict`: dict of Model instances used by this list, grouped by Model type
                * :obj:`list` of :obj:`str`: list of errors

        Raises:
            :obj:`ParsedExpressionError`: if :obj:`model_cls` does not have a :obj:`Meta` attribute
        """
        self.__reset_tokenization()

        if not self.expression:
            self.errors.append('Expression cannot be empty')
            return (None, None, self.errors)

        # detect and report bad tokens
        bad_tokens = set()
        for tok in self._py_tokens:
            if tok.exact_type not in self.LEGAL_TOKENS:
                if tok.string and tok.string != ' ':
                    bad_tokens.add(tok.string)
                else:
                    bad_tokens.add(token.tok_name[tok.type])
        if bad_tokens:
            self.errors.append("'{}', a {}.{}, contains bad token(s): '{}'".format(
                self.expression, self.model_cls.__name__,
                self.attr, "', '".join(bad_tokens)))
            return (None, None, self.errors)

        idx = 0
        while idx < len(self._py_tokens):

            # categorize token codes
            obj_tables_token_code = ObjTablesTokenCodes.other
            if self._py_tokens[idx].type == token.OP:
                obj_tables_token_code = ObjTablesTokenCodes.op
            elif self._py_tokens[idx].type == token.NUMBER:
                obj_tables_token_code = ObjTablesTokenCodes.number

            # a token that isn't an identifier needs no processing
            if self._py_tokens[idx].type != token.NAME:
                # record non-identifier token
                self._obj_tables_tokens.append(ObjTablesToken(obj_tables_token_code, self._py_tokens[idx].string))
                idx += 1
                continue

            matches = []
            tmp_errors = []
            for get_obj_tables_lex_el in [self._get_related_obj_id, self._get_disambiguated_id, self._get_func_call_id]:
                result = get_obj_tables_lex_el(idx, case_fold_match=case_fold_match)
                if result is not None:
                    if isinstance(result, str):
                        tmp_errors.append(result)
                    elif isinstance(result, LexMatch):
                        matches.append(result)
                    else:   # pragma no cover
                        raise ParsedExpressionError("Result is neither str nor LexMatch '{}'".format(result))

            # should find either matches or errors
            if not (matches or tmp_errors):
                raise ParsedExpressionError("No matches or errors found in '{}'".format(self.expression))
            # if only errors are found, break to return them
            if tmp_errors and not matches:
                self.errors = tmp_errors
                break

            # matches is a list of LexMatch, if it contains one longest match, use that, else report error
            # sort matches by Python token pattern length
            matches_by_length = sorted(matches, key=lambda lex_match: lex_match.num_py_tokens)
            longest_length = matches_by_length[-1].num_py_tokens
            longest_matches = []
            while matches_by_length and matches_by_length[-1].num_py_tokens == longest_length:
                longest_matches.append(matches_by_length.pop())
            if len(longest_matches) > 1:
                raise ParsedExpressionError("Multiple longest matches: '{}'".format(longest_matches))

            # good match
            # advance idx to the next token
            # record match data in self._obj_tables_tokens and self.related_objects
            match = longest_matches.pop()
            idx += match.num_py_tokens
            obj_tables_tokens = match.obj_tables_tokens
            self._obj_tables_tokens.extend(obj_tables_tokens)
            for obj_tables_token in obj_tables_tokens:
                if obj_tables_token.code == ObjTablesTokenCodes.obj_id:
                    self.related_objects[obj_tables_token.model_type][obj_tables_token.model_id] = obj_tables_token.model

        # detect ambiguous tokens
        valid_function_names = [func.__name__ for func in self.valid_functions]
        for obj_tables_token in self._obj_tables_tokens:
            if obj_tables_token.code in [ObjTablesTokenCodes.obj_id, ObjTablesTokenCodes.math_func_id]:
                matching_items = []

                for model_type in self.term_models:
                    if obj_tables_token.token_string in self._objs.get(model_type, {}):
                        matching_items.append(model_type.__name__)

                if obj_tables_token.token_string in valid_function_names:
                    matching_items.append('function')

                if len(matching_items) > 1:
                    self.errors.append('ObjTablesToken `{}` is ambiguous. ObjTablesToken matches a {} and a {}.'.format(
                        obj_tables_token.token_string, ', a '.join(matching_items[0:-1]), matching_items[-1]))

        if self.errors:
            return (None, None, self.errors)
        try:
            self._compiled_expression, self._compiled_namespace = self._compile()
        except SyntaxError as error:
            return (None, None, ['SyntaxError: ' + str(error)])

        self._compiled_expression_with_units, self._compiled_namespace_with_units = self._compile(with_units=True)
        return (self._obj_tables_tokens, self.related_objects, None)

    def test_eval(self, values=1., with_units=False):
        """ Test evaluate this :obj:`ParsedExpression` with the value of all models given by :obj:`values`

        This is used to validate this :obj:`ParsedExpression`, as well as for testing.

        Args:
            values (:obj:`float` or :obj:`dict`, optional): value(s) of models used by the test
                evaluation; if a scalar, then that value is used for all models; if a :obj:`dict` then
                it maps model types to their values, or it maps model types to dictionaries that map
                model ids to the values of individual models used in the test
            with_units (:obj:`bool`, optional): if :obj:`True`, evaluate units

        Returns:
            :obj:`float`, :obj:`int`, or :obj:`bool`: the value of the expression

        Raises:
            :obj:`ParsedExpressionError`: if the expression evaluation fails
        """
        def constant_factory(value):
            return lambda: value

        if isinstance(values, (int, float, bool)):
            obj_values = {}
            for model_type in self.related_objects.keys():
                obj_values[model_type] = collections.defaultdict(constant_factory(values))
        else:
            obj_values = {}
            for model_type, model_values in values.items():
                if isinstance(model_values, (int, float, bool)):
                    obj_values[model_type] = collections.defaultdict(constant_factory(model_values))
                else:
                    obj_values[model_type] = model_values

        return self.eval(obj_values, with_units=with_units)

    def eval(self, values, with_units=False):
        """ Evaluate the expression

        Approach:

            1. Ensure that the expression is compiled
            2. Prepare namespace with model values
            3. :obj:`eval` the Python expression

        Args:
            values (:obj:`dict`): dictionary that maps model types to dictionaries that
                map model ids to values
            with_units (:obj:`bool`, optional): if :obj:`True`, include units

        Returns:
            :obj:`float`, :obj:`int`, or :obj:`bool`: the value of the expression

        Raises:
            :obj:`ParsedExpressionError`: if the expression has not been compiled or the evaluation fails
        """
        if with_units:
            expression = self._compiled_expression_with_units
            namespace = self._compiled_namespace_with_units
        else:
            expression = self._compiled_expression
            namespace = self._compiled_namespace

        if not expression:
            raise ParsedExpressionError("Cannot evaluate '{}', as it not been successfully compiled".format(
                self.expression))

        # prepare name space
        for model_type, model_id_values in values.items():
            namespace[model_type.__name__] = model_id_values

        for model_type, model_ids in self.related_objects.items():
            if hasattr(model_type.Meta, 'expression_term_model') and model_type.Meta.expression_term_model:
                namespace[model_type.__name__] = {}
                for id, model in model_ids.items():
                    namespace[model_type.__name__][id] = model.expression._parsed_expression.eval(values)
            elif hasattr(model_type.Meta, 'expression_term_value') and model_type.Meta.expression_term_value:
                namespace[model_type.__name__] = {}
                for id, model in model_ids.items():
                    namespace[model_type.__name__][id] = getattr(model, model_type.Meta.expression_term_value)

        if with_units:
            for model_type, model_ids in self.related_objects.items():
                for id, model in model_ids.items():
                    if isinstance(namespace[model_type.__name__][id], bool):
                        namespace[model_type.__name__][id] = float(namespace[model_type.__name__][id])
                    units = getattr(model, model.Meta.expression_term_units)
                    if units is None:
                        raise ParsedExpressionError('Units must be defined')
                    if not isinstance(units, self.unit_registry.Unit):
                        raise ParsedExpressionError('Unsupported units "{}"'.format(units))
                    namespace[model_type.__name__][id] *= self.unit_registry.parse_expression(str(units))

        # prepare error message
        error_suffix = " cannot eval expression '{}' in {}; ".format(self.expression,
                                                                     self.model_cls.__name__)

        # evaluate compiled expression
        try:
            return eval(expression, {}, namespace)
        except SyntaxError as error:
            raise ParsedExpressionError("SyntaxError:" + error_suffix + str(error))
        except NameError as error:
            raise ParsedExpressionError("NameError:" + error_suffix + str(error))
        except Exception as error:
            raise ParsedExpressionError("Exception:" + error_suffix + str(error))

    def _compile(self, with_units=False):
        """ Compile expression for evaluation by :obj:`eval` method

        Args:
            with_units (:obj:`bool`, optional): if :obj:`True`, include units

        Returns:
            :obj:`tuple`:

                * :obj:`str`: compiled expression for :obj:`eval`
                * :obj:`dict`: compiled namespace
        """

        str_expression = self.get_str(self._obj_tables_token_to_str, with_units=with_units)
        compiled_expression = compile(str_expression, '<ParsedExpression>', 'eval')

        compiled_namespace = {func.__name__: func for func in self.valid_functions}
        if with_units and self.unit_registry:
            compiled_namespace['__dimensionless__'] = self.unit_registry.parse_expression('dimensionless')

        return compiled_expression, compiled_namespace

    def _obj_tables_token_to_str(self, token):
        """ Get a string representation of a token that represents an instance of :obj:`Model`

        Args:
            token (:obj:`ObjTablesToken`): token that represents an instance of :obj:`Model`

        Returns:
            :obj:`str`: string representation of a token that represents an instance of :obj:`Model`.
        """
        return '{}["{}"]'.format(token.model_type.__name__, token.model.get_primary_attribute())

    def get_str(self, obj_tables_token_to_str, with_units=False, number_units=' * __dimensionless__'):
        """ Generate string representation of expression, e.g. for evaluation by :obj:`eval`

        Args:
            obj_tables_token_to_str (:obj:`callable`): method to get string representation of a token
                that represents an instance of :obj:`Model`.
            with_units (:obj:`bool`, optional): if :obj:`True`, include units
            number_units (:obj:`str`, optional): default units for numbers

        Returns:
            :obj:`str`: string representation of expression

        Raises:
            :obj:`ParsedExpressionError`: if the expression is invalid
        """
        if not self._obj_tables_tokens:
            raise ParsedExpressionError("Cannot evaluate '{}', as it not been successfully tokenized".format(
                self.expression))

        tokens = []
        idx = 0
        while idx < len(self._obj_tables_tokens):
            obj_tables_token = self._obj_tables_tokens[idx]
            if obj_tables_token.code == ObjTablesTokenCodes.obj_id:
                val = obj_tables_token_to_str(obj_tables_token)
                tokens.append(val)
            elif obj_tables_token.code == ObjTablesTokenCodes.number:
                if with_units:
                    tokens.append(obj_tables_token.token_string + number_units)
                else:
                    tokens.append(obj_tables_token.token_string)
            else:
                tokens.append(obj_tables_token.token_string)
            idx += 1

        return ' '.join(tokens)

    def __str__(self):
        rv = []
        rv.append("model_cls: {}".format(self.model_cls.__name__))
        rv.append("expression: '{}'".format(self.expression))
        rv.append("attr: {}".format(self.attr))
        rv.append("py_tokens: {}".format("'" + "', '".join([t.string for t in self._py_tokens]) + "'"))
        rv.append("related_objects: {}".format(self.related_objects))
        rv.append("errors: {}".format(self.errors))
        rv.append("obj_tables_tokens: {}".format(self._obj_tables_tokens))
        return '\n'.join(rv)


class LinearParsedExpressionValidator(object):
    """ Verify whether a :obj:`ParsedExpression` is equivalent to a linear function of variables

    A linear function of identifiers has the form `a1 * v1 + a2 * v2 + ... an * vn`, where
    `a1 ... an` are floats or integers and `v1 ... vn` are variables.

    Attributes:
        expression (:obj:`str`): the expression
        parsed_expression (:obj:`ParsedExpression`): parsed_expression
        tree (:obj:`_ast.Expression`): the abstract syntax tree
        is_linear (:obj:`boolean`): whether the :obj:`ParsedExpression` is linear
        opaque_ids (:obj:`dict`): map from Model ids that are not valid Python identifiers to opaque ids
        next_id (:obj:`int`): suffix of next opaque Python identifier
    """
    # TODO (APG): use random expressions made by RandomExpression to test more extensively
    # TODO (APG): transform expressions to std. polynomial form so set_lin_coeffs() works on all linear exprs

    # TODO (APG): make portable to 3.8 <= Python, which replaces ast.Num with ast.Constant
    VALID_NOTE_TYPES = set([ast.Constant,
                            ast.Num,
                            ast.UnaryOp,    # only UAdd and USub
                            ast.UAdd,
                            ast.USub,
                            ast.BinOp,      # only Add, Sub and Mult
                            ast.Add,
                            ast.Sub,
                            ast.Mult,
                            ast.Load,
                            ast.Name,
    ])

    VALID_PYTHON_ID_PREFIX = 'opaque_model_id_'

    def __init__(self):
        self.opaque_ids = {}
        self.next_id = 1

    def _init(self, expression):
        """ Initialize expression attributes

        Args:
            expression (:obj:`str`): expression

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        self.expression = expression.strip()
        if not self.expression:
            raise ValueError("Cannot validate empty expression")
        return self

    def validate(self, parsed_expression, set_linear_coeffs=True):
        """ Determine whether the expression is a valid linear expression

        Args:
            parsed_expression (:obj:`ParsedExpression`): a parsed expression
            set_linear_coeffs (:obj:`boolean`, optional): if :obj:`True`:, set the linear coefficients
                for the related objects

        Returns:
            :obj:`tuple`: :obj:`(False, error)` if :obj:`self.expression` does not represent a linear expression,
                or :obj:`(True, None)` if it does
        """
        self.parsed_expression = parsed_expression
        self._init(self._expr_with_python_ids(parsed_expression))
        rv = self._validate()
        if set_linear_coeffs:
            self._set_lin_coeffs()
        return rv

    def _convert_model_id_to_python_id(self, model_id):
        """ If a model id isn't a valid Python identifier, convert it

        Args:
            model_id (:obj:`str`): a model id, which might not be a Python identifier

        Returns:
            :obj:`str`: `model_id` if it was a Python identifier, otherwise an opaque Python identifier
        """
        if model_id.isidentifier() and not keyword.iskeyword(model_id):
            return model_id
        if model_id in self.opaque_ids:
            return self.opaque_ids[model_id]
        next_opaque_id = f"{self.VALID_PYTHON_ID_PREFIX}{self.next_id}"
        self.opaque_ids[model_id] = next_opaque_id
        self.next_id += 1
        return next_opaque_id

    def _expr_with_python_ids(self, parsed_expression):
        """ Obtain an expression with valid Python identifiers

        Args:
            parsed_expression (:obj:`ParsedExpression`): a parsed expression

        Returns:
            :obj:`str`: an expression with valid Python identifiers
        """
        expr = []
        for obj_tables_token in parsed_expression._obj_tables_tokens:
            if obj_tables_token.code == ObjTablesTokenCodes.obj_id:
                expr.append(self._convert_model_id_to_python_id(obj_tables_token.model_id))
            else:
                expr.append(obj_tables_token.token_string)
        return ' '.join(expr)

    def _validate(self):
        """ Determine whether `self.expression` is a valid linear expression

        Returns:
            :obj:`tuple`: :obj:`(False, error)` if :obj:`self.expression` does not represent a linear expression,
                or :obj:`(True, None)` if it does
        """
        # Approach:
        # Use ast to parse the expression
        # If the expression contains terms that are not allowed in a linear expression, return False
        # If the expression contains numbers that cannot be coerced into floats, return False
        # Simplify the expression by:
        #   Removing unary operators
        #   Multiplying constants in all products
        #   Distributing multiplication over all addition and subtraction terms
        #   Multiplying constants in all products, again
        # If the expression contains contains constant terms, return False
        # If the expression contains contains products of variables, return False
        # If the expression contains contains constants to the right of variables, return False

        self.is_linear = False

        valid, error = self._validate_syntax()
        if not valid:
            return (valid, error)

        valid, error = self._validate_node_types()
        if not valid:
            return (valid, error)

        valid, error = self._validate_nums()
        if not valid:
            return (valid, error)

        self._remove_unary_operators()
        self._multiply_numbers()
        self._dist_mult()
        self._multiply_numbers()
        self._remove_subtraction()
        self._multiply_numbers()

        if self._expr_has_a_constant():
            return (False, f"expression '{self.expression}' contains constant term(s)")

        if self._expr_has_products_of_variables():
            return (False, f"expression '{self.expression}' contains product(s) of variables")

        if self._expr_has_constant_right_of_vars():
            return (False, f"expression '{self.expression}' contains a constant right of a var in a product")

        self.is_linear = True
        return (True, None)

    def _validate_syntax(self):
        """ Parse the expression and determine whether it is valid Python

        Returns:
            :obj:`tuple`: :obj:`(False, error)` if :obj:`self.expression` is not valid Python,
                or :obj:`(True, None)` if it is
        """
        try:
            self.tree = ast.parse(self.expression, mode='eval')
        except SyntaxError:
            return (False, f"Python syntax error in '{self.expression}'")
        return (True, None)

    def _validate_node_types(self):
        """ Determine whether the expression contains terms allowed in a linear expression

        Returns:
            :obj:`tuple`: :obj:`(False, error)` if :obj:`self.expression` contains terms not allowed
                in a linear expression, or :obj:`(True, None)` if it does not
        """
        errors = set()
        for node in ast.walk(self.tree.body):
            if type(node) not in self.VALID_NOTE_TYPES:
                errors.add(type(node).__name__)
        if errors:
            return (False, f"contains invalid terms {str(errors)}")
        return (True, None)

    def _validate_nums(self):
        """ Determine whether all numbers in the expression can be coerced into floats

        Returns:
            :obj:`tuple`: :obj:`(False, error)` if :obj:`self.expression` has numbers that cannot be
                coerced into floats, or :obj:`(True, None)` if it does not
        """
        errors = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Num):
                try:
                    val = float(node.n)
                except TypeError as e:
                    errors.append(str(e))
        if errors:
            return (False, str(errors))
        return (True, None)


    class DistributeMult(ast.NodeTransformer):
        # transform (x + y)*z to x*z + y*z, and (x - y)*(3 + 5) to x*(3 + 5) - y*(3 + 5), and
        # transform x*(3 + 5) to x * 3 + x * 5, and (x * y)*(3 + 5) to (x * y)*3 + (x * y)*5
        def visit_BinOp(self, node):
            self.generic_visit(node)
            if (isinstance(node.op, ast.Mult) and
                (isinstance(node.right, (ast.Num, ast.Name, ast.UnaryOp, ast.BinOp)) and
                 (isinstance(node.left, ast.BinOp) and
                  isinstance(node.left.op, (ast.Add, ast.Sub))))):
                # form: (a +/- b) * c
                left = ast.BinOp(left=node.left.left,
                                 op=ast.Mult(),
                                 right=node.right)
                right = ast.BinOp(left=node.left.right,
                                  op=ast.Mult(),
                                  right=node.right)
                return ast.BinOp(left=left,
                                 right=right,
                                 op=node.left.op)
            if (isinstance(node.op, ast.Mult) and
                (isinstance(node.left, (ast.Num, ast.Name, ast.UnaryOp, ast.BinOp)) and
                 (isinstance(node.right, ast.BinOp) and
                  isinstance(node.right.op, (ast.Add, ast.Sub))))):
                # form: a * (b +/- c)
                left = ast.BinOp(left=node.left,
                                 op=ast.Mult(),
                                 right=node.right.left)
                right = ast.BinOp(left=node.left,
                                  op=ast.Mult(),
                                  right=node.right.right)
                return ast.BinOp(left=left,
                                 right=right,
                                 op=node.right.op)
            return node


    def _dist_mult(self):
        """ Distribute multiplication over addition and subtraction terms

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        while True:
            # iterate until all multiplication in the ast has been distributed
            tree_copy = copy.deepcopy(self.tree)
            self.DistributeMult().visit(self.tree)
            if self.ast_eq(tree_copy, self.tree):
                break
        return self


    class MoveCoeffsToLeft(ast.NodeTransformer):
        # transform r_for*5 - 2*r_back*4 to 5*r_for - 2 * 4 * r_back
        def visit_BinOp(self, node):
            self.generic_visit(node)
            if (isinstance(node.op, ast.Mult) and
                (isinstance(node.left, ast.Name) or
                 (isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Mult))) and
                isinstance(node.right, ast.Num)):
                # swap Name on left with Num on right
                return ast.BinOp(left=node.right,
                                 op=ast.Mult(),
                                 right=node.left)
            return node


    def _move_coeffs_to_left(self):
        """ Move numerical coefficients to the left of names (variables)

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        # Not used -- causes failures in wc_lang tests
        # TODO (APG): use to transform arbitrary expressions into canonical polynomial form
        while True:
            # iterate until all coefficients have been moved
            tree_copy = copy.deepcopy(self.tree)
            self.MoveCoeffsToLeft().visit(self.tree)
            if self.ast_eq(tree_copy, self.tree):
                break
        return self


    class DistributeSub(ast.NodeTransformer):
        # transform subtraction into addition, with -1 * distributed over the subtrahend
        # e.g., transform "r - (2 * r_for - 2 * r_back)" to "r + (-1 * 2 * r_for - -1 * 2 * r_back)"
        def visit_BinOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Sub):
                if isinstance(node.right, (ast.Name, ast.Num)):
                    # subtrahend is Num or Name
                    return ast.BinOp(left=node.left,
                                     op=ast.Add(),
                                     right=ast.BinOp(left=ast.Num(-1),
                                                     op=ast.Mult(),
                                                     right=node.right))
                if isinstance(node.right, ast.BinOp) and isinstance(node.right.op, (ast.Add, ast.Sub)):
                    # subtrahend is addition or subtraction
                    right = ast.BinOp(left=ast.BinOp(left=ast.Num(-1),
                                                     op=ast.Mult(),
                                                     right=node.right.left),
                                      op=node.right.op,
                                      right=ast.BinOp(left=ast.Num(-1),
                                                     op=ast.Mult(),
                                                     right=node.right.right))
                    return ast.BinOp(left=node.left,
                                     op=ast.Add(),
                                     right=right)
                if isinstance(node.right, ast.BinOp) and isinstance(node.right.op, ast.Mult):
                    # subtrahend is multiplication
                    right = ast.BinOp(left=ast.BinOp(left=ast.Num(-1),
                                                     op=ast.Mult(),
                                                     right=node.right.left),
                                      op=node.right.op,
                                      right=node.right.right)
                    return ast.BinOp(left=node.left,
                                     op=ast.Add(),
                                     right=right)
            return node

    def _remove_subtraction(self):
        """ Remove subtraction by converting it to -1 times the subtrahend

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        self.DistributeSub().visit(self.tree)
        return self


    class MultiplyNums(ast.NodeTransformer):
        # transform 2 * 4 * x - 3.0 * -2. * y to 8 * x - -6.0 * y
        def visit_BinOp(self, node):
            self.generic_visit(node)
            # multiply adjacent constants
            if (isinstance(node.op, ast.Mult) and
                isinstance(node.left, ast.Num) and
                isinstance(node.right, ast.Num)):
                return ast.Num(node.left.n * node.right.n)
            # multiply constants in node.left and node.right.left
            if (isinstance(node.op, ast.Mult) and
                isinstance(node.left, ast.Num) and
                isinstance(node.right, ast.BinOp) and
                isinstance(node.right.op, ast.Mult) and
                isinstance(node.right.left, ast.Num)):
                return ast.BinOp(left=ast.Num(node.left.n * node.right.left.n),
                                 op=ast.Mult(),
                                 right=node.right.right)
            return node


    def _multiply_numbers(self):
        """ Multiply numbers in a product

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        self.MultiplyNums().visit(self.tree)
        return self


    class RemoveUnaryOps(ast.NodeTransformer):
        # transform +anything to anything, and -anything to -1 * anything
        def visit_UnaryOp(self, node):
            self.generic_visit(node)
            # transform +anything to anything
            if isinstance(node.op, ast.UAdd):
                return node.operand
            # transform -anything to -1 * anything
            if isinstance(node.op, ast.USub):
                return ast.BinOp(left=ast.Num(-1),
                                 op=ast.Mult(),
                                 right=node.operand)


    def _remove_unary_operators(self):
        """ Remove unary operators

        Returns:
            :obj:`LinearParsedExpressionValidator`: :obj:`self`, so this operation can be chained
        """
        self.RemoveUnaryOps().visit(self.tree)
        return self

    def _expr_has_a_constant(self):
        """ Determine whether the expression contains a constant term

        The expression must be transformed by removing unary operators, distribing multiplication,
        moving coeffecients left, and multiplying numbers before calling this.

        Returns:
            :obj:`boolean`: whether the expression contains a constant term
        """
        nodes = list(ast.walk(self.tree.body))
        if len(nodes) == 1 and isinstance(nodes[0], ast.Num):
            return True
        for node in ast.walk(self.tree.body):
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                if isinstance(node.left, ast.Num) or isinstance(node.right, ast.Num):
                    return True
        return False

    @staticmethod
    def _num_of_variables_in_a_product(node):
        """ Count all variables in a product of terms rooted at `node`
        """
        num_variables = 0
        if isinstance(node.op, ast.Mult):
            if isinstance(node.left, ast.Name):
                num_variables += 1
            elif isinstance(node.left, ast.BinOp):
                num_variables += LinearParsedExpressionValidator._num_of_variables_in_a_product(node.left)
            if isinstance(node.right, ast.Name):
                num_variables += 1
            elif isinstance(node.right, ast.BinOp):
                num_variables += LinearParsedExpressionValidator._num_of_variables_in_a_product(node.right)
        return num_variables

    def _max_num_variables_in_a_product(self):
        """ Determine the maximum number of variables in a product in this `LinearParsedExpressionValidator`

        Returns:
            :obj:`float`: the maximum number of variables in a product in this `LinearParsedExpressionValidator`
        """
        max_num_variables_in_a_product = 0
        for node in ast.walk(self.tree.body):
            if isinstance(node, ast.BinOp):
                max_num_variables_in_a_product = max(max_num_variables_in_a_product,
                                                     self._num_of_variables_in_a_product(node))
        return max_num_variables_in_a_product

    def _expr_has_products_of_variables(self):
        """ Determine whether the expression contains products of variables

        The expression must be transformed by distribing multiplication before calling this.

        Returns:
            :obj:`boolean`: whether the expression contains products of variables
        """
        return 1 < self._max_num_variables_in_a_product()

    @staticmethod
    def _product_has_name(node):
        """ Whether a product of terms rooted at `node` contains a name

        Returns:
            :obj:`boolean`: whether a product of terms rooted at `node` contains a name
        """
        if isinstance(node, ast.Name):
            return True
        rv = False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            rv = rv | LinearParsedExpressionValidator._product_has_name(node.left)
            rv = rv | LinearParsedExpressionValidator._product_has_name(node.right)
        return rv

    @staticmethod
    def _product_has_num(node):
        """ Whether a product of terms rooted at `node` contains a number

        Returns:
            :obj:`boolean`: whether a product of terms rooted at `node` contains a number
        """
        if isinstance(node, ast.Num):
            return True
        rv = False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            rv = rv | LinearParsedExpressionValidator._product_has_num(node.left)
            rv = rv | LinearParsedExpressionValidator._product_has_num(node.right)
        return rv

    def _expr_has_constant_right_of_vars(self):
        """ Determine whether the expression contains constants to the right of a variable in a product

        The expression must be transformed by removing unary operators, and distribing multiplication
        before calling this.

        Returns:
            :obj:`boolean`: whether the expression contains constants to the right of a variable in a product
        """
        for node in ast.walk(self.tree.body):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                if (LinearParsedExpressionValidator._product_has_name(node.left) and
                    LinearParsedExpressionValidator._product_has_num(node.right)):
                    return True
        return False

    def get_cls_and_model(self, id):
        """ Get the class and instance of a related model with id `id`

        Args:
            id (:obj:`str`): id

        Returns:
            :obj:`tuple`: :obj:`(:obj:`type`, :obj:`Model`)`: the class and instance of a related model with id `id`

        Raises:
            :obj:`ParsedExpressionError`: if multiple related models have id `id`
        """
        # TODO (APG): properly handle related objects with conflicting ids:
        # Approach to handle class-distinguished model ids (ModCls.id) that disambiguate conflicting ids:
        # 1. At initialization in _init(), use parsed_expression.related_objects to map all names (ids) to related objects 
        # 2. To simplify all ast transformations and scans, internally transform class-distinguished model ids
        #    and invalid Python identifiers into simple, unique names, and record the mapping (replace self.opaque_ids)
        # 3. Process the ast as usual
        # 4. Discard this method
        # 5. In _get_coeffs_for_vars(), after walking the ast, remap all ids back to the original names in the return value
        # 6. In _set_lin_coeffs(), use the map made in #1 to map all types of model ids to class & model
        cls, model = None, None
        for related_class, related_objs in self.parsed_expression.related_objects.items():
            for related_obj in related_objs.values():
                if related_obj.id == id:
                    if cls is not None:
                        raise ParsedExpressionError(f"multiple models with id='{id}' in expression '{self.expression}'")
                    cls, model = related_class, related_obj
        return cls, model

    def _get_coeffs_for_vars(self):
        """ Get coefficients for variables in a linear expression in standard form

        Returns:
            :obj:`list`: of :obj:`(:obj:`float`, :obj:`str`)`: coefficient and model id pairs
        """
        coeff_model_id_pairs = []
        default_coeff = 1
        coeff = default_coeff
        for node in ast.walk(self.tree.body):
            if isinstance(node, ast.Num):
                coeff = node.n
            if isinstance(node, ast.Name):
                coeff_model_id_pairs.append((float(coeff), node.id))
                coeff = default_coeff

        # remap ids of objects whose ids aren't Python identifiers by using self.opaque_ids
        inverted_opaque_ids = {opaque_id: id for id, opaque_id in self.opaque_ids.items()}
        coeff_model_id_pairs_original_ids = []
        for coeff, model_id in coeff_model_id_pairs:
            if model_id in inverted_opaque_ids:
                coeff_model_id_pairs_original_ids.append((coeff, inverted_opaque_ids[model_id]))
            else:
                coeff_model_id_pairs_original_ids.append((coeff, model_id))
        return coeff_model_id_pairs_original_ids

    def _set_lin_coeffs(self):
        """ Set the linear coefficients for the related objects

        Assumes `_validate()` has been called
        """
        model_cls = self.parsed_expression.model_cls

        if self.is_linear:
            default_val = 0.
        else:
            default_val = float('nan')

        self.parsed_expression.lin_coeffs = lin_coeffs = {}

        for attr_name, attr in model_cls.Meta.attributes.items():
            if (isinstance(attr, RelatedAttribute) and
                attr.related_class.__name__ in model_cls.Meta.expression_term_models):
                lin_coeffs[attr.related_class] = {}

        for related_class, related_objs in self.parsed_expression.related_objects.items():
            for related_obj in related_objs.values():
                lin_coeffs[related_class][related_obj] = default_val

        if not self.is_linear:
            return

        for coeff, var_id in self._get_coeffs_for_vars():
            model_cls, model = self.get_cls_and_model(var_id)
            lin_coeffs[model_cls][model] += coeff

    @staticmethod
    def ast_eq(ast1, ast2):
        """ Are two abstract syntax trees equal
        """
        return astor.dump_tree(ast1) == astor.dump_tree(ast2)
