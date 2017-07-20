""" Database-independent Django-like object model

:Author: Jonathan Karr <karr@mssm.edu>
:Author: Arthur Goldberg <Arthur.Goldberg@mssm.edu>
:Date: 2016-12-12
:Copyright: 2016, Karr Lab
:License: MIT
"""

from __future__ import print_function
from collections import Iterable, OrderedDict, defaultdict
from datetime import date, time, datetime
from enum import Enum
from itertools import chain
from math import floor, isnan
from natsort import natsort_keygen, natsorted, ns
from operator import attrgetter, methodcaller
from six import integer_types, string_types, with_metaclass
from stringcase import sentencecase
from os.path import basename, dirname, splitext
from weakref import WeakSet, WeakKeyDictionary
from wc_utils.util.list import is_sorted
from wc_utils.util.misc import quote, OrderableNone
from wc_utils.util.string import indent_forest
from wc_utils.util.types import get_subclasses, get_superclasses
import copy
import dateutil.parser
import inflect
import re
import sys
import warnings
# todo: simplify primary attributes, deserialization
# todo: improve memory efficiency
# todo: improve run-time
# todo: improve naming: on meaning for Model, clean -> convert, Slug -> id, etc.
# todo: implement schema migration


class ModelMeta(type):

    def __new__(metacls, name, bases, namespace):
        """
        Args:
            metacls (:obj:`Model`): `Model`, or a subclass of `Model`
            name (:obj:`str`): `Model` class name
            bases (:obj: `tuple`): tuple of superclasses
            namespace (:obj:`dict`): namespace of `Model` class definition
        """

        # terminate early so this method is only run on the subclasses of `Model`
        if name == 'Model' and len(bases) == 1 and bases[0] is object:
            return super(ModelMeta, metacls).__new__(metacls, name, bases, namespace)

        # Create new Meta internal class if not provided in class definition so
        # that each model has separate internal Meta classes
        if 'Meta' not in namespace:
            Meta = namespace['Meta'] = type('Meta', (Model.Meta,), {})

            Meta.attribute_order = []
            for base in bases:
                if issubclass(base, Model):
                    for attr_name in base.Meta.attribute_order:
                        if attr_name not in Meta.attribute_order:
                            Meta.attribute_order.append(attr_name)
            Meta.attribute_order = tuple(Meta.attribute_order)

            Meta.unique_together = copy.deepcopy(bases[0].Meta.unique_together)
            Meta.indexed_attrs_tuples = copy.deepcopy(bases[0].Meta.indexed_attrs_tuples)
            Meta.tabular_orientation = bases[0].Meta.tabular_orientation
            Meta.frozen_columns = bases[0].Meta.frozen_columns
            Meta.ordering = copy.deepcopy(bases[0].Meta.ordering)

        # validate attributes
        metacls.validate_related_attributes(name, bases, namespace)

        # validate attribute inheritance
        metacls.validate_attribute_inheritance(name, bases, namespace)

        # call super class method
        cls = super(ModelMeta, metacls).__new__(metacls, name, bases, namespace)

        # Initialize meta data
        # todo: move all validation in the below methods to the metaclass
        metacls.init_inheritance(cls)

        metacls.init_attributes(cls)

        metacls.init_primary_attribute(cls)

        cls.Meta.related_attributes = OrderedDict()
        for model in get_subclasses(Model):
            metacls.init_related_attributes(cls, model)

        metacls.init_attribute_order(cls)

        metacls.init_ordering(cls)

        metacls.init_verbose_names(cls)

        metacls.validate_attributes(cls)

        metacls.create_model_manager(cls)

        # Return new class
        return cls

    def init_inheritance(cls):
        """ Get tuple of this model and superclasses which are subclasses of `Model` """
        cls.Meta.inheritance = tuple([cls] + [supercls for supercls in get_superclasses(cls)
                                              if issubclass(supercls, Model) and supercls is not Model])

    @classmethod
    def validate_related_attributes(metacls, name, bases, namespace):
        """ Check the related attributes

        Raises:
            :obj:`ValueError`: if an :obj:`OneToManyAttribute` or :obj:`ManyToOneAttribute` has a `related_name` equal to its `name`
        """
        for attr_name, attr in namespace.items():
            if isinstance(attr, (OneToManyAttribute, ManyToOneAttribute)) and attr.related_name == attr_name:
                raise ValueError('The related name of {} {} cannot be equal to its name'.format(attr.__class__.__name__, attr_name))

    @classmethod
    def validate_attribute_inheritance(metacls, name, bases, namespace):
        """ Check attribute inheritance

        Raises:
            :obj:`ValueError`: if subclass overrides a superclass attribute (instance of Attribute) with an incompatible
                attribute (i.e. an attribute that is not a subclass of the class of the super class' attribute)
        """
        for attr_name, attr in namespace.items():
            for super_cls in bases:
                if attr_name in dir(super_cls):
                    super_attr = getattr(super_cls, attr_name)
                    if (isinstance(attr, Attribute) or isinstance(super_attr, Attribute)) and not isinstance(attr, super_attr.__class__):
                        raise ValueError('Attribute "{}" of class "{}" inherited from "{}" must be a subclass of {} because the attribute is already defined in the superclass'.
                                         format(__name__, super_cls.__name__, attr_name, super_attr.__class__.__name__))

    def init_attributes(cls):
        """ Initialize attributes """

        cls.Meta.attributes = OrderedDict()
        for attr_name in sorted(dir(cls)):
            orig_attr = getattr(cls, attr_name)

            if isinstance(orig_attr, Attribute):
                if attr_name in cls.__dict__:
                    attr = orig_attr
                else:
                    attr = copy.copy(orig_attr)

                attr.name = attr_name
                if not attr.verbose_name:
                    attr.verbose_name = sentencecase(attr_name)
                cls.Meta.attributes[attr_name] = attr

                if isinstance(attr, RelatedAttribute) and attr.name in cls.__dict__:
                    attr.primary_class = cls

    def init_related_attributes(cls, model_cls):
        """ Initialize related attributes

        Raises:
            :obj:`ValueError`: if related attributes of the class are not valid
                (e.g. if a class that is the subject of a relationship does not have a primary attribute)
        """
        for attr in model_cls.Meta.attributes.values():
            if isinstance(attr, RelatedAttribute):

                # deserialize related class references by class name
                if isinstance(attr.related_class, string_types):
                    related_class_name = attr.related_class
                    if '.' not in related_class_name:
                        related_class_name = model_cls.__module__ + '.' + related_class_name

                    related_class = get_model(related_class_name)
                    if related_class:
                        attr.related_class = related_class

                # setup related attributes on related classes
                if attr.name in model_cls.__dict__ and attr.related_name and \
                        isinstance(attr.related_class, type) and issubclass(attr.related_class, Model):
                    related_classes = chain([attr.related_class], get_subclasses(attr.related_class))
                    for related_class in related_classes:
                        # check that name doesn't conflict with another attribute
                        if attr.related_name in related_class.Meta.attributes and \
                            not (isinstance(attr, (OneToOneAttribute, ManyToManyAttribute)) and attr.related_name == attr.name):
                            other_attr = related_class.Meta.attributes[attr.related_name]
                            raise ValueError('Related attribute {}.{} cannot use the same related name as {}.{}'.format(
                                model_cls.__name__, attr.name,
                                related_class.__name__, attr.related_name,
                            ))

                        # check that name doesn't clash with another related attribute from a different model
                        if attr.related_name in related_class.Meta.related_attributes and \
                                related_class.Meta.related_attributes[attr.related_name] is not attr:
                            other_attr = related_class.Meta.related_attributes[attr.related_name]
                            raise ValueError('Attributes {}.{} and {}.{} cannot use the same related attribute name {}.{}'.format(
                                model_cls.__name__, attr.name,
                                other_attr.primary_class.__name__, other_attr.name,
                                related_class.__name__, attr.related_name,
                            ))

                        # add attribute to dictionary of related attributes
                        related_class.Meta.related_attributes[attr.related_name] = attr
                        related_class.Meta.related_attributes = OrderedDict(
                            sorted(related_class.Meta.related_attributes.items(), key=lambda x: x[0]))

    def init_primary_attribute(cls):
        """ Initialize the primary attribute of a model

        Raises:
            :obj:`ValueError`: if class has multiple primary attributes
        """
        primary_attributes = [attr for attr in cls.Meta.attributes.values() if attr.primary]

        if len(primary_attributes) == 0:
            cls.Meta.primary_attribute = None

        elif len(primary_attributes) == 1:
            cls.Meta.primary_attribute = primary_attributes[0]

        else:
            raise ValueError('Model {} cannot have more than one primary attribute'.format(cls.__name__))

    def init_attribute_order(cls):
        """ Initialize the order in which the attributes should be printed across Excel columns """
        ordered_attributes = list(cls.Meta.attribute_order or ())

        unordered_attributes = set()
        for base in cls.Meta.inheritance:
            for attr_name in base.__dict__.keys():
                if isinstance(getattr(base, attr_name), Attribute) and attr_name not in ordered_attributes:
                    unordered_attributes.add(attr_name)

        unordered_attributes = natsorted(unordered_attributes, alg=ns.IGNORECASE)

        cls.Meta.attribute_order = tuple(ordered_attributes + unordered_attributes)

    def init_ordering(cls):
        """ Initialize how to sort objects """
        if not cls.Meta.ordering:
            if cls.Meta.primary_attribute:
                cls.Meta.ordering = (cls.Meta.primary_attribute.name, )
            else:
                cls.Meta.ordering = ()

    def init_verbose_names(cls):
        """ Initialize the singular and plural verbose names of a model """
        if not cls.Meta.verbose_name:
            cls.Meta.verbose_name = sentencecase(cls.__name__)

            if not cls.Meta.verbose_name_plural:
                inflect_engine = inflect.engine()
                cls.Meta.verbose_name_plural = sentencecase(inflect_engine.plural(cls.__name__))

        elif not cls.Meta.verbose_name_plural:
            inflect_engine = inflect.engine()
            cls.Meta.verbose_name_plural = inflect_engine.plural(cls.Meta.verbose_name)

    def validate_n_normalize_attr_tuples(cls, attribute):
        """ Validate and normalize a tuple of tuples of attribute names

        Args:
            attribute (:obj:`str`): the name of the attribute to validate and normalize

        Raises:
            :obj:`ValueError`: if attributes are not valid
        """
        # getattr(cls.Meta, attribute) should be a tuple of tuples of attribute names
        if not isinstance(getattr(cls.Meta, attribute), tuple):
            raise ValueError("{} for '{}' must be a tuple, not '{}'".format(
                attribute, cls.__name__, getattr(cls.Meta, attribute)))

        for tup_of_attrnames in getattr(cls.Meta, attribute):
            if not isinstance(tup_of_attrnames, tuple):
                raise ValueError("{} for '{}' must be a tuple of tuples, not '{}'".format(
                attribute, cls.__name__, getattr(cls.Meta, attribute)))

            for attr_name in tup_of_attrnames:
                if not isinstance(attr_name, str):
                    raise ValueError("{} for '{}' must be a tuple of tuples of strings, not '{}'".format(
                attribute, cls.__name__, getattr(cls.Meta, attribute)))

                if attr_name not in cls.Meta.attributes and attr_name not in cls.Meta.related_attributes:
                    raise ValueError("{} for '{}' must be a tuple of tuples of attribute names, "
                        "not '{}'".format(attribute, cls.__name__, getattr(cls.Meta, attribute)))

            if len(set(tup_of_attrnames)) < len(tup_of_attrnames):
                raise ValueError("{} for '{}' cannot repeat attribute names "
                    "in any tuple: '{}'".format(attribute, cls.__name__, getattr(cls.Meta, attribute)))

        # raise errors if multiple tup_of_attrnames are equivalent
        tup_of_attrnames_map = defaultdict(list)
        for tup_of_attrnames in getattr(cls.Meta, attribute):
            tup_of_attrnames_map[frozenset(tup_of_attrnames)].append(tup_of_attrnames)
        equivalent_tuples = []
        for equivalent_tup_of_attrnames in tup_of_attrnames_map.values():
            if 1<len(equivalent_tup_of_attrnames):
                equivalent_tuples.append(equivalent_tup_of_attrnames)
        if 0<len(equivalent_tuples):
            raise ValueError("{} cannot contain identical attribute sets: {}".format(
                attribute, str(equivalent_tuples)))

        # Normalize each tup_of_attrnames as a sorted tuple
        setattr(cls.Meta, attribute,
            ModelMeta.normalize_tuple_of_tuples_of_attribute_names(getattr(cls.Meta, attribute)))

    @staticmethod
    def normalize_tuple_of_tuples_of_attribute_names(tuple_of_tuples_of_attribute_names):
        """ Normalize a tuple of tuples of attribute names by sorting each member tuple

        Enables simple indexing and searching of tuples

        Args:
            tuple_of_tuples_of_attribute_names (:obj:`tuple`): a tuple of tuples of attribute names

        Returns:
            :obj:`tuple`: a tuple of sorted tuples of attribute names
        """
        normalized_tup_of_attrnames = []
        for tup_of_attrnames in tuple_of_tuples_of_attribute_names:
            normalized_tup_of_attrnames.append(tuple(sorted(tup_of_attrnames)))
        return tuple(normalized_tup_of_attrnames)

    def validate_attributes(cls):
        """ Validate attribute values

        Raises:
            :obj:`ValueError`: if attributes are not valid
        """
        # `attribute_order` is a tuple of attribute names
        if not isinstance(cls.Meta.attribute_order, tuple):
            raise ValueError('attribute_order for {} must be a tuple'.format(cls.__name__))

        for attr_name in cls.Meta.attribute_order:
            if not isinstance(attr_name, str):
                raise ValueError("attribute_order for {} must contain attribute names; '{}' is "
                                 "not a string".format(cls.__name__, attr_name))

            if attr_name not in cls.Meta.attributes:
                raise ValueError("attribute_order must contain attribute names; '{}' not found in "
                "attributes of {}: {}".format(attr_name, cls.__name__, set(cls.Meta.attributes.keys())))

        cls.validate_n_normalize_attr_tuples('unique_together')
        cls.validate_n_normalize_attr_tuples('indexed_attrs_tuples')

    def create_model_manager(cls):
        """ Create a `Manager` for this `Model`

        The `Manager` is accessed via a `Model`'s `objects` attribute

        Attributes:
            cls (:obj:`Class`): the `Model` class which is being managed
        """
        setattr(cls, 'objects', Manager(cls))


class Manager(object):
    """ Enable O(1) dictionary-based searching of a Model's instances

    This class is inspired by Django's `Manager` class. An instance of :obj:`Manger` is associated with
    each :obj:`Model` and accessed as the class attribute `objects` (as in Django).
    The tuples of attributes to index are specified by the `indexed_attrs_tuples` attribute of
    `core.Model.Meta`, which contains a tuple of tuples of attributes to index.
    `Model`s with empty `indexed_attrs_tuples` attributes incur no overhead from `Manager`.

    :obj:`Manager` maintains a dictionary for each indexed attribute tuple, and a reverse index from each
    :obj:`Model` instance to its indexed attribute tuple keys.

    These data structures support
    * O(1) get operations for `Model` instances indexed by a indexed attribute tuple
    * O(1) `Model` instance insert and update operations

    Attributes:
        cls (:obj:`Class`): the :obj:`Model` class which is being managed
        _new_instances (:obj:`WeakSet`): set of all new instances of `cls` that have not been indexed,
            stored as weakrefs, so `Model`'s that are otherwise unused can be garbage collected
        _index_dicts (:obj:`dict` mapping `tuple` to :obj:`WeakSet`): indices that enable
            lookup of :obj:`Model` instances from their `Meta.indexed_attrs_tuples`
            mapping: <attr names tuple> -> <attr values tuple> -> WeakSet(<model_obj instances>)
        _reverse_index (:obj:`WeakKeyDictionary` mapping :obj:`Model` instance to :obj:`dict`): a reverse
            index that provides all of each :obj:`Model`'s indexed attribute tuple keys
            mapping: <model_obj instances> -> <attr names tuple> -> <attr values tuple>
        num_ops_since_gc (:obj:`int`): number of operations since the last gc of weaksets
    """
    # todo: learn how to describe dict -> dict -> X in Sphinx

    # number of Manager operations between calls to _gc_weaksets
    # todo: make this value configurable
    GC_PERIOD = 1000
    def __init__(self, cls):
        self.cls = cls
        if self.cls.Meta.indexed_attrs_tuples:
            self._new_instances = WeakSet()
            self._create_indices()
            self.num_ops_since_gc = 0

    def _check_model(self, model_obj, method):
        """ Verify `model_obj`'s `Model`

        Args:
            model_obj (:obj:`Model`): a `Model` instance
            method (:obj:`str`): the name of the method requesting the check

        Raises:
            :obj:`ValueError`: if `model_obj`'s type is not handled by this `Manager`
            :obj:`ValueError`: if `model_obj`'s type does not have any indexed attribute tuples
        """
        if not type(model_obj) is self.cls:
            raise ValueError("{}(): The '{}' Manager does not process '{}' objects".format(
                method, self.cls.__name__, type(model_obj).__name__))
        if not self.cls.Meta.indexed_attrs_tuples:
            raise ValueError("{}(): The '{}' Manager does not have any indexed attribute tuples".format(
                method, self.cls.__name__))

    def _create_indices(self):
        """ Create dicts needed to manage indices on attribute tuples

        The references to :obj:`Model` instances are stored as weakrefs in a :obj:`WeakKeyDictionary`, so that
        :obj:`Model`'s which are otherwise unused get garbage collected.
        """
        self._index_dicts = {}
        # for each indexed_attrs, create a dict
        for indexed_attrs in self.cls.Meta.indexed_attrs_tuples:
            self._index_dicts[indexed_attrs] = {}

        # A reverse index from Model instances to index keys enables updates of instances that
        # are already indexed. Update is performed by deleting and inserting.
        self._reverse_index = WeakKeyDictionary()

    def _dump_index_dicts(self):
        # gc before printing to produce consistent data
        self._gc_weaksets()
        print("Dicts for '{}':".format(self.cls.__name__))
        for attr_tuple,d in self._index_dicts.items():
            print('\tindexed attr tuple:', attr_tuple)
            for k,v in d.items():
                print('\t\tk,v', k, {id(obj_model) for obj_model in v})
        print("Reverse dicts for '{}':".format(self.cls.__name__))
        for obj,attr_keys in self._reverse_index.items():
            print("\tmodel at {}".format(id(obj)))
            for indexed_attrs,vals in attr_keys.items():
                print("\t\t'{}' is '{}'".format(indexed_attrs,vals))

    @staticmethod
    def _get_attr_tuple_vals(model_obj, attr_tuple):
        """ Provide the values of the attributes in `attr_tuple`

        Args:
            model_obj (:obj:`Model`): a `Model` instance
            attr_tuple (:obj:`tuple`): a tuple of attribute names in `model_obj`

        Returns:
            :obj:`tuple`: `model_obj`'s values for the attributes in `attr_tuple`
        """
        return tuple(map(lambda name: getattr(model_obj, name), attr_tuple))

    @staticmethod
    def _get_hashable_values(values):
        """ Provide hashable values for a tuple of values of a `Model`'s attributes

        Args:
            values (:obj:`tuple`): values of `Model` attributes

        Returns:
            :obj:`tuple`: hashable values for a `tuple` of values of `Model` attributes
        """
        if isinstance(values, string_types):
            raise ValueError("_get_hashable_values does not take a string: '{}'".format(values))
        if not isinstance(values, Iterable):
            raise ValueError("_get_hashable_values takes an iterable, not: '{}'".format(values))
        hashable_values = []
        for val in values:
            if isinstance(val, RelatedManager):
                hashable_values.append(tuple(sorted([id(sub_val) for sub_val in val])))
            elif isinstance(val, Model):
                hashable_values.append(id(val))
            else:
                hashable_values.append(val)
        return tuple(hashable_values)

    @staticmethod
    def _hashable_attr_tup_vals(model_obj, attr_tuple):
        """ Provide hashable values for the attributes in `attr_tuple`

        Args:
            model_obj (:obj:`Model`): a `Model` instance
            attr_tuple (:obj:`tuple`): a tuple of attribute names in `model_obj`

        Returns:
            :obj:`tuple`: hashable values for `model_obj`'s attributes in `attr_tuple`
        """
        return Manager._get_hashable_values(Manager._get_attr_tuple_vals(model_obj, attr_tuple))

    def _get_attribute_types(self, model_obj, attr_names):
        """ Provide the attribute types for a tuple of attribute names

        Args:
            model_obj (:obj:`Model`): a `Model` instance
            attr_names (:obj:`tuple`): a tuple of attribute names in `model_obj`

        Returns:
            :obj:`tuple`: `model_obj`'s attribute types for the attribute name(s) in `attr_names`
        """
        self._check_model(model_obj, '_get_attribute_types')
        if isinstance(attr_names, string_types):
            raise ValueError("_get_attribute_types(): attr_names cannot be a string: '{}'".format(attr_names))
        if not isinstance(attr_names, Iterable):
            raise ValueError("_get_attribute_types(): attr_names must be an iterable, not: '{}'".format(attr_names))
        cls = self.cls
        types = []
        for attr_name in attr_names:
            if attr_name in cls.Meta.attributes:
                attr = cls.Meta.attributes[attr_name]
            elif attr_name in cls.Meta.related_attributes:
                attr = cls.Meta.related_attributes[attr_name]
            else:
                raise ValueError("Cannot find '{}' in attribute names for '{}'".format(attr_name,
                    cls.__name__))
            types.append(attr)
        return tuple(types)

    def _register_obj(self, model_obj):
        """ Register the `Model` instance `model_obj`

        Called by `Model.__init__()`. Do nothing if `model_obj`'s `Model` has no indexed attribute tuples.

        Args:
            model_obj (:obj:`Model`): a new `Model` instance
        """
        if self.cls.Meta.indexed_attrs_tuples:
            self._check_model(model_obj, '_register_obj')
            self._run_gc_weaksets()
            self._new_instances.add(model_obj)

    def _update(self, model_obj):
        """ Update the indices for `model_obj`, whose indexed attribute have been updated

        Costs O(I) where I is the number of indexed attribute tuples for `model_obj`.

        Args:
            model_obj (:obj:`Model`): a `Model` instance
        """
        self._check_model(model_obj, '_update')
        self._run_gc_weaksets()
        cls = self.cls
        if model_obj not in self._reverse_index:
            raise ValueError("Can't _update an instance of '{}' that is not in the _reverse_index".format(
                cls.__name__))
        self._delete(model_obj)
        self._insert(model_obj)

    def _delete(self, model_obj):
        """ Delete an `model_obj` from the indices

        Args:
            model_obj (:obj:`Model`): a `Model` instance
        """
        self._check_model(model_obj, '_delete')
        for indexed_attr_tuple,vals in self._reverse_index[model_obj].items():
            if vals in self._index_dicts[indexed_attr_tuple]:
                self._index_dicts[indexed_attr_tuple][vals].remove(model_obj)
                # Recover memory by deleting empty WeakSets.
                # Empty WeakSets formed by automatic removal of weak refs are gc'ed by _gc_weaksets.
                if 0 == len(self._index_dicts[indexed_attr_tuple][vals]):
                    del self._index_dicts[indexed_attr_tuple][vals]
        del self._reverse_index[model_obj]

    def _insert_new(self, model_obj):
        """ Insert a new `model_obj` into the indices that are used to search on indexed attribute tuples

        Args:
            model_obj (:obj:`Model`): a `Model` instance
        """
        self._check_model(model_obj, '_insert_new')
        if model_obj not in self._new_instances:
            raise ValueError("Cannot _insert_new() an instance of '{}' that is not new".format(self.cls.__name__))
        self._insert(model_obj)
        self._new_instances.remove(model_obj)

    def _insert(self, model_obj):
        """ Insert `model_obj` into the indices that are used to search on indexed attribute tuples

        Costs O(I) where I is the number of indexed attribute tuples for the `Model`.

        Args:
            model_obj (:obj:`Model`): a `Model` instance
        """
        self._check_model(model_obj, '_insert')
        self._run_gc_weaksets()
        cls = self.cls

        for indexed_attr_tuple in cls.Meta.indexed_attrs_tuples:
            vals = Manager._hashable_attr_tup_vals(model_obj, indexed_attr_tuple)
            if vals not in self._index_dicts[indexed_attr_tuple]:
                self._index_dicts[indexed_attr_tuple][vals] = WeakSet()
            self._index_dicts[indexed_attr_tuple][vals].add(model_obj)

        d = {}
        for indexed_attr_tuple in cls.Meta.indexed_attrs_tuples:
            d[indexed_attr_tuple] = Manager._hashable_attr_tup_vals(model_obj, indexed_attr_tuple)
        self._reverse_index[model_obj] = d

    def _run_gc_weaksets(self):
        """ Periodically garbage collect empty WeakSets

        Returns:
            :obj:`int`: number of empty WeakSets deleted
        """
        self.num_ops_since_gc += 1
        if Manager.GC_PERIOD <= self.num_ops_since_gc:
            self.num_ops_since_gc = 0
            return self._gc_weaksets()
        return 0

    def _gc_weaksets(self):
        """ Garbage collect empty WeakSets formed by deletion of weak refs to `Model` instances with no strong refs

        Returns:
            :obj:`int`: number of empty WeakSets deleted
        """
        num = 0
        for indexed_attr_tuple,attr_val_dict in self._index_dicts.items():
            # do not change attr_val_dict while iterating
            attr_val_weakset_pairs = list(attr_val_dict.items())
            for attr_val,weakset in attr_val_weakset_pairs:
                if not weakset:
                    del self._index_dicts[indexed_attr_tuple][attr_val]
                    num += 1
        return num

    # Public Manager() methods follow
    # If the Model is not indexed these methods do nothing (and return None if a value is returned)
    def all(self):
        """ Provide all instances of the `Model` managed by this `Manager`

        Returns:
            :obj:`list` of `Model`: a list of all instances of the managed `Model`
            or `None` if the `Model` is not indexed
        """
        if self.cls.Meta.indexed_attrs_tuples:
            self._run_gc_weaksets()
            # return list of strong refs, so keys in WeakKeyDictionary cannot be changed by gc
            # while iterating over them
            return list(self._reverse_index.keys())
        else:
            return None

    def upsert(self, model_obj):
        """ Update the indices for `model_obj` that are used to search on indexed attribute tuples

        `Upsert` means update or insert. Update the indices if `model_obj` is already stored, otherwise
        insert `model_obj`.

        Costs O(I) where I is the number of indexed attribute tuples for the `Model`.

        Args:
            model_obj (:obj:`Model`): a `Model` instance
        """
        if self.cls.Meta.indexed_attrs_tuples:
            if model_obj in self._new_instances:
                self._insert_new(model_obj)
            else:
                self._update(model_obj)

    def upsert_all(self):
        """ Upsert the indices for all of this `Manager`'s `Model`'s
        """
        if self.cls.Meta.indexed_attrs_tuples:
            for model_obj in self.all():
                self.upsert(model_obj)

    def insert_all_new(self):
        """ Insert all new instances of this `Manager`'s `Model`'s into the search indices
        """
        if self.cls.Meta.indexed_attrs_tuples:
            for model_obj in self._new_instances:
                self._insert(model_obj)
            self._new_instances.clear()

    def clear_new_instances(self):
        """ Clear the set of new instances that have not been inserted
        """
        if self.cls.Meta.indexed_attrs_tuples:
            self._new_instances.clear()

    def get(self, **kwargs):
        """ Get the `Model` instance(s) that match the attribute name,value pair(s) in `kwargs`

        The keys in `kwargs` must correspond to an entry in the `Model`'s `indexed_attrs_tuples`.
        Warning: this method is non-deterministic. To obtain `Manager`'s O(1) performance, `Model`
        instances in the index are stored in `WeakSet`s. Therefore, the order of elements in the list
        returned is not reproducible. Applications that need reproducibility must deterministically
        order elements in lists returned by this method.

        Args:
            kwargs (:obj:`dict`): keyword args mapping from attribute name(s) to value(s)

        Returns:
            :obj:`list` of `Model`: a list of `Model` instances whose indexed attribute tuples have the
            values in `kwargs`; otherwise `None`, indicating no match

        Raises:
            :obj:`ValueError`: if the attribute name(s) in `kwargs.keys()` do not correspond to an
            indexed attribute tuple of the `Model`
        """
        cls = self.cls

        if 0==len(kwargs.keys()):
            raise ValueError("No arguments provided in get() on '{}'".format(cls.__name__))
        if not self.cls.Meta.indexed_attrs_tuples:
            return None

        # searching for an indexed_attrs instance
        # Sort by attribute names, to obtain the normalized order for attributes in an indexed_attrs_tuples.
        # This normalization is performed by
        # ModelMeta.normalize_tuple_of_tuples_of_attribute_names during ModelMeta.__new__()
        keys, vals = zip(*sorted(kwargs.items()))
        possible_indexed_attributes = keys
        if possible_indexed_attributes not in self._index_dicts:
            raise ValueError("{} not an indexed attribute tuple in '{}'".format(possible_indexed_attributes,
                cls.__name__))
        if vals not in self._index_dicts[possible_indexed_attributes]:
            return None
        if 0 == len(self._index_dicts[possible_indexed_attributes][vals]):
            return None
        return list(self._index_dicts[possible_indexed_attributes][vals])


class TabularOrientation(Enum):
    """ Describes a table's orientation

    * `row`: the first row contains attribute names; subsequents rows store objects
    * `column`: the first column contains attribute names; subsequents columns store objects
    * `inline`: a cell contains a table, as a comma-separated list for example
    """
    row = 1
    column = 2
    inline = 3


class Model(with_metaclass(ModelMeta, object)):
    """ Base object model

    Attributes:
        _source (:obj:`ModelSource`): file location, worksheet, column, and row where the object was defined
        objects (:obj:`Manager`): a `Manager` that supports searching for `Model` instances
    """

    class Meta(object):
        """ Meta data for :class:`Model`

        Attributes:
            attributes (:obj:`OrderedDict` of `Attribute`): attributes
            related_attributes (:obj:`set` of `Attribute`): attributes declared in related objects
            primary_attribute (:obj:`Attribute`): attribute with `primary` = `True`
            unique_together (:obj:`tuple` of :obj:`tuple`'s of attribute names): controls what tuples of
                attribute values must be unique
            indexed_attrs_tuples (:obj:`tuple` of `tuple`'s of attribute names): tuples of attributes on
                which instances of this `Model` will be indexed by the `Model`'s `Manager`
            attribute_order (:obj:`tuple` of `str`): tuple of attribute names, in the order in which they should be displayed
            verbose_name (:obj:`str`): verbose name to refer to an instance of the model
            verbose_name_plural (:obj:`str`): plural verbose name for multiple instances of the model
            tabular_orientation (:obj:`TabularOrientation`): orientation of model objects in table (e.g. Excel)
            frozen_columns (:obj:`int`): number of Excel columns to freeze
            inheritance (:obj:`tuple` of `class`): tuple of all superclasses
            ordering (:obj:`tuple` of attribute names): controls the order in which objects should be printed when serialized
        """
        attributes = None
        related_attributes = None
        primary_attribute = None
        unique_together = ()
        indexed_attrs_tuples = ()
        attribute_order = ()
        verbose_name = ''
        verbose_name_plural = ''
        tabular_orientation = TabularOrientation.row
        frozen_columns = 1
        inheritance = None
        ordering = None

    def __init__(self, **kwargs):
        """
        Args:
            **kwargs (:obj:`dict`, optional): dictionary of keyword arguments with keys equal to the names of the model attributes

        Raises:
            :obj:`TypeError`: if keyword argument is not a defined attribute
        """

        """ check that related classes of attributes are defined """
        self.validate_related_attributes()

        """ initialize attributes """
        # attributes
        for attr in self.Meta.attributes.values():
            super(Model, self).__setattr__(attr.name, attr.get_init_value(self))

        # related attributes
        for attr in self.Meta.related_attributes.values():
            super(Model, self).__setattr__(attr.related_name, attr.get_related_init_value(self))

        """ set attribute values """
        # attributes
        for attr in self.Meta.attributes.values():
            if attr.name not in kwargs:
                default = attr.get_default(self)
                setattr(self, attr.name, default)

        # attributes
        for attr in self.Meta.related_attributes.values():
            if attr.related_name not in kwargs:
                default = attr.get_related_default(self)
                if default:
                    setattr(self, attr.related_name, default)

        # process arguments
        for attr_name, val in kwargs.items():
            if attr_name not in self.Meta.attributes and attr_name not in self.Meta.related_attributes:
                raise TypeError("'{:s}' is an invalid keyword argument for {}.__init__".format(
                    attr_name, self.__class__.__name__))
            setattr(self, attr_name, val)

        self._source = None

        # register this Model instance with the class' Manager
        self.__class__.objects._register_obj(self)

    @classmethod
    def validate_related_attributes(cls):
        """ Validate attribute values

        Raises:
            :obj:`ValueError`: if related attributes are not valid (e.g. if a class that is the subject of a relationship does not have a primary attribute)
        """

        for attr_name, attr in cls.Meta.attributes.items():
            if isinstance(attr, RelatedAttribute) and not (isinstance(attr.related_class, type) and issubclass(attr.related_class, Model)):
                raise ValueError('Related class {} of {}.{} must be defined'.format(
                    attr.related_class, attr.primary_class.__name__, attr_name))

        # tabular orientation
        if cls.Meta.tabular_orientation == TabularOrientation.inline:
            for attr in cls.Meta.related_attributes.values():
                if attr in [OneToManyAttribute, OneToManyAttribute, ManyToOneAttribute, ManyToManyAttribute]:
                    raise ValueError(
                        'Inline model "{}" must define their own serialization/deserialization methods'.format(cls.__name__))

                if 'deserialize' not in attr.__class__.__dict__:
                    raise ValueError(
                        'Inline model "{}" must define their own serialization/deserialization methods'.format(cls.__name__))

            if len(cls.Meta.related_attributes) == 0:
                raise ValueError(
                    'Inline model "{}" should have a single required related one-to-one or one-to-many attribute'.format(cls.__name__))
            elif len(cls.Meta.related_attributes) == 1:
                attr = list(cls.Meta.related_attributes.values())[0]

                if not isinstance(attr, (OneToOneAttribute, OneToManyAttribute)):
                    warnings.warn(
                        'Inline model "{}" should have a single required related one-to-one or one-to-many attribute'.format(cls.__name__), SchemaWarning)

                elif attr.min_related == 0:
                    warnings.warn(
                        'Inline model "{}" should have a single required related one-to-one or one-to-many attribute'.format(cls.__name__), SchemaWarning)
            else:
                warnings.warn(
                    'Inline model "{}" should have a single required related one-to-one or one-to-many attribute'.format(cls.__name__), SchemaWarning)

    def __setattr__(self, attr_name, value, propagate=True):
        """ Set attribute and validate any unique attribute constraints

        Args:
            attr_name (:obj:`str`): attribute name
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change through attribute `set_value` and `set_related_value`
        """
        if propagate:
            if attr_name in self.__class__.Meta.attributes:
                attr = self.__class__.Meta.attributes[attr_name]
                value = attr.set_value(self, value)

            elif attr_name in self.__class__.Meta.related_attributes:
                attr = self.__class__.Meta.related_attributes[attr_name]
                value = attr.set_related_value(self, value)

        super(Model, self).__setattr__(attr_name, value)

    def normalize(self):
        """ Normalize an object into a canonical form. Specifically, this method sorts the RelatedManagers into a canonical order because their
        order has no semantic meaning. Importantly, this canonical form is reproducible. Thus, this canonical form facilitates reproducible
        computations on top of :obj:`Model` objects.

        Raises:
            :obj:`ValueError`: if object is not reproducibly normalizable
        """

        self._generate_normalize_sort_keys()

        normalized_objs = []
        objs_to_normalize = [self]

        while objs_to_normalize:
            obj = objs_to_normalize.pop()
            if obj not in normalized_objs:
                normalized_objs.append(obj)

                for attr_name, attr in chain(obj.Meta.attributes.items(), obj.Meta.related_attributes.items()):
                    if isinstance(attr, RelatedAttribute):
                        val = getattr(obj, attr_name)
                        
                        # normalize children
                        if isinstance(val, list):
                            objs_to_normalize.extend(val)
                        elif val:
                            objs_to_normalize.append(val)

                        # sort
                        if isinstance(val, list) and len(val) > 1:
                            if attr_name in obj.Meta.attributes:
                                cls = attr.related_class
                            else:
                                cls = attr.primary_class

                            val.sort(key=cls._normalize_sort_key)

    @classmethod
    def _generate_normalize_sort_keys(cls):
        """ Generates keys for sorting the class """
        generated_keys = []
        keys_to_generate = [cls]
        while keys_to_generate:
            cls = keys_to_generate.pop()
            if cls not in generated_keys:
                generated_keys.append(cls)

                cls._normalize_sort_key = cls._generate_normalize_sort_key()

                for attr in cls.Meta.attributes.values():
                    if isinstance(attr, RelatedAttribute):
                        keys_to_generate.append(attr.related_class)

                for attr in cls.Meta.related_attributes.values():
                    if isinstance(attr, RelatedAttribute):
                        keys_to_generate.append(attr.primary_class)

    @classmethod
    def _generate_normalize_sort_key(cls):
        """ Generates key for sorting the class """

        # single unique attribute
        for attr_name, attr in cls.Meta.attributes.items():
            if attr.unique:
                def key(obj):
                    val = getattr(obj, attr_name)
                    if val is None:
                        return OrderableNone
                    return val
                return key

        # tuple of attributes that are unique together
        if cls.Meta.unique_together:
            lens = [len(x) for x in cls.Meta.unique_together]
            i_shortest = lens.index(min(lens))

            def key(obj):
                vals = []
                for attr_name in cls.Meta.unique_together[i_shortest]:
                    val = getattr(obj, attr_name)
                    if isinstance(val, RelatedManager):
                        vals.append((subval.serialize() for subval in val))
                    elif isinstance(val, Model):
                        vals.append(val.serialize())
                    else:
                        vals.append(val)
                return tuple(vals)
            return key

        # include all attributes
        def key(obj):
            vals = []
            for attr_name in chain(cls.Meta.attributes.keys(), cls.Meta.related_attributes.keys()):
                val = getattr(obj, attr_name)
                if isinstance(val, RelatedManager):
                    subvals_serial = []
                    for subval in val:
                        subval_serial = subval.__class__._normalize_sort_key(subval)
                        if subval_serial is None:
                            subval_serial = OrderableNone
                        subvals_serial.append(subval_serial)
                    vals.append(tuple(sorted(subvals_serial)))
                elif isinstance(val, Model):
                    vals.append(val.__class__._normalize_sort_key(val))
                else:
                    vals.append(OrderableNone if val is None else val)
            return tuple(vals)
        return key

    def is_equal(self, other):
        """ Determine if two objects are semantically equal

        Args:
            other (:obj:`Model`): object to compare

        Returns:
            :obj:`bool`: `True` if objects are semantically equal, else `False`
        """

        """
        todo: this can potentially be sped up by

        #. Flattening the object graphs
        #. Sorting the flattening object lists
        #. comparing the flattened lists item-by-item
        """

        self.normalize()
        other.normalize()

        checked_pairs = []
        pairs_to_check = [(self, other, )]
        while pairs_to_check:
            pair = pairs_to_check.pop()
            obj, other_obj = pair
            if pair not in checked_pairs:
                checked_pairs.append(pair)

                # non-related attributes
                if not obj._is_equal_attributes(other_obj):
                    return False

                # related attributes
                for attr_name, attr in chain(obj.Meta.attributes.items(), obj.Meta.related_attributes.items()):
                    if isinstance(attr, RelatedAttribute):
                        val = getattr(obj, attr_name)
                        other_val = getattr(other_obj, attr_name)

                        if val.__class__ != other_val.__class__:
                            return False

                        if val is None:
                            pass
                        elif isinstance(val, Model):
                            pairs_to_check.append((val, other_val, ))
                        elif len(val) != len(other_val):
                            return False
                        else:
                            if attr_name in obj.Meta.attributes:
                                cls = attr.related_class
                            else:
                                cls = attr.primary_class

                            for v, ov in zip(val, other_val):
                                pairs_to_check.append((v, ov, ))

        return True

    def _is_equal_attributes(self, other):
        """ Determine if the attributes of two objects are semantically equal

        Args:
            other (:obj:`Model`): object to compare

        Returns:
            :obj:`bool`: `True` if the objects' attributes are semantically equal, else `False`
        """
        # objects are the same
        if self is other:
            return True

        # check objects are of the same class
        if not self.__class__ is other.__class__:
            return False

        # check that their non-related attributes are semantically equal
        for attr_name, attr in chain(self.Meta.attributes.items(), self.Meta.related_attributes.items()):
            val = getattr(self, attr_name)
            other_val = getattr(other, attr_name)

            if not isinstance(attr, RelatedAttribute):
                if not attr.value_equal(val, other_val):
                    return False

            elif isinstance(val, RelatedManager):
                if len(val) != len(other_val):
                    return False

            else:
                if val is None and other_val is not None:
                    return False

        return True

    def __str__(self):
        """ Get the string representation of an object

        Returns:
            :obj:`str`: string representation of object
        """

        if self.__class__.Meta.primary_attribute:
            return '<{}.{}: {}>'.format(self.__class__.__module__, self.__class__.__name__, getattr(self, self.__class__.Meta.primary_attribute.name))

        return super(Model, self).__str__()

    def set_source(self, path_name, sheet_name, attribute_seq, row):
        """ Set metadata about source of the file, worksheet, columns, and row where the object was defined

        Args:
            path_name (:obj:`str`): pathname of source file for object
            sheet_name (:obj:`str`): name of spreadsheet containing source data for object
            attribute_seq (:obj:`list`): sequence of attribute names in source file; blank values
                indicate attributes that were ignored
            row (:obj:`int`): row number of object in its source file
        """
        self._source = ModelSource(path_name, sheet_name, attribute_seq, row)

    def get_source(self, attr_name):
        """ Get file location of attribute with name `attr_name`

        Provide the type, filename, worksheet, row, and column of `attr_name`. Row and column use
        1-based counting. Column is provided in Excel format if the file was a spreadsheet.

        Args:
            attr_name (:obj:`str`): attribute name

        Returns:
            tuple of (type, basename, worksheet, row, column)

        Raises:
            ValueError if the location of `attr_name` is unknown
        """
        if self._source is None:
            raise ValueError("location information unavailable".format())

        # account for the header row and possible transposition
        row = self._source.row
        try:
            column = self._source.attribute_seq.index(attr_name) + 1
        except ValueError as e:
            raise ValueError("cannot find attr with name {}".format(attr_name))
        if self.Meta.tabular_orientation == TabularOrientation.column:
            column, row = row, column
        path = self._source.path_name
        sheet_name = self._source.sheet_name

        _, ext = splitext(path)
        ext = ext.split('.')[-1]
        if 'xlsx' in ext:
            col = excel_col_name(column)
            return (ext, quote(basename(path)), quote(sheet_name), row, col)
        else:
            return (ext, quote(basename(path)), quote(sheet_name), row, column)

    @classmethod
    def sort(cls, objects):
        """ Sort list of `Model` objects

        Args:
            objects (:obj:`list` of `Model`): list of objects

        Returns:
            :obj:`list` of `Model`: sorted list of objects
        """
        if cls.Meta.ordering:
            return natsorted(objects, cls.get_sort_key, alg=ns.IGNORECASE)

    @classmethod
    def get_sort_key(cls, object):
        """ Get sort key for `Model` instance `object` based on `cls.Meta.ordering`

        Args:
            object (:obj:`Model`): `Model` instance

        Returns:
            :obj:`object` or `tuple` of `object`: sort key for `object`
        """
        vals = []
        for attr_name in cls.Meta.ordering:
            if attr_name[0] == '-':
                increasing = False
                attr_name = attr_name[1:]
            else:
                increasing = True
            attr = cls.Meta.attributes[attr_name]
            val = attr.serialize(getattr(object, attr_name))
            if increasing:
                vals.append(val)
            else:
                vals.append(-val)

        if len(vals) == 1:
            return val
        return tuple(vals)

    def difference(self, other):
        """ Get the semantic difference between two objects

        Args:
            other (:obj:`Model`): other object

        Returns:
            :obj:`str`: difference message
        """

        total_difference = {}
        checked_pairs = []
        pairs_to_check = [(self, other, total_difference)]
        while pairs_to_check:
            obj, other_obj, difference = pairs_to_check.pop()
            pair = (obj, other_obj, )

            if pair in checked_pairs:
                continue
            checked_pairs.append(pair)

            # initialize structure to store differences
            difference['objects'] = (obj, other_obj, )

            # types
            if obj.__class__ is not other_obj.__class__:
                difference['type'] = 'Objects {} and {} have different types "{}" and "{}"'.format(
                    obj, other_obj, obj.__class__, other_obj.__class__)
                continue

            # attributes
            difference['attributes'] = {}

            for attr_name, attr in chain(obj.Meta.attributes.items(), obj.Meta.related_attributes.items()):
                val = getattr(obj, attr_name)
                other_val = getattr(other_obj, attr_name)

                if not isinstance(attr, RelatedAttribute):
                    if not attr.value_equal(val, other_val):
                        difference['attributes'][attr_name] = '{} != {}'.format(val, other_val)

                elif isinstance(val, RelatedManager):
                    if len(val) != len(other_val):
                        difference['attributes'][attr_name] = 'Length: {} != Length: {}'.format(
                            len(val), len(other_val))
                    else:
                        serial_vals = sorted(((v.serialize(), v) for v in val), key=lambda x: x[0])
                        serial_other_vals = sorted(((v.serialize(), v) for v in other_val), key=lambda x: x[0])

                        i_val = 0
                        oi_val = 0
                        difference['attributes'][attr_name] = []
                        while i_val < len(val) and oi_val < len(other_val):
                            serial_v = serial_vals[i_val][0]
                            serial_ov = serial_other_vals[oi_val][0]
                            if serial_v == serial_ov:
                                el_diff = {}
                                difference['attributes'][attr_name].append(el_diff)
                                pairs_to_check.append((serial_vals[i_val][1], serial_other_vals[oi_val][1], el_diff))
                                i_val += 1
                                oi_val += 1
                            elif serial_v < serial_ov:
                                difference['attributes'][attr_name].append('No matching element {}'.format(serial_v))
                                i_val += 1
                            else:
                                oi_val += 1

                        for i_val2 in range(i_val, len(val)):
                            difference['attributes'][attr_name].append(
                                'No matching element {}'.format(serial_vals[i_val2][0]))
                elif val is None:
                    if other_val is not None:
                        difference['attributes'][attr_name] = '{} != {}'.format(val, other_val.serialize())
                elif other_val is None:
                    difference['attributes'][attr_name] = '{} != {}'.format(val.serialize(), other_val)
                else:
                    difference['attributes'][attr_name] = {}
                    pairs_to_check.append((val, other_val, difference['attributes'][attr_name], ))

        return self._render_difference(self._simplify_difference(total_difference))

    def _simplify_difference(self, difference):
        """ Simplify difference data structure

        Args:
            difference (:obj:`dict`): representation of the semantic difference between two objects
        """

        to_flatten = [[difference, ], ]
        while to_flatten:
            diff_hierarchy = to_flatten.pop()
            if not diff_hierarchy:
                continue

            cur_diff = diff_hierarchy[-1]

            if not cur_diff:
                continue

            if 'type' in cur_diff:
                continue

            new_to_flatten = []
            flatten_again = False
            for attr, val in list(cur_diff['attributes'].items()):
                if isinstance(val, dict):
                    if val:
                        new_to_flatten.append(diff_hierarchy + [val])
                elif isinstance(val, list):
                    for v in reversed(val):
                        if v:
                            if isinstance(v, dict):
                                new_to_flatten.append(diff_hierarchy + [v])
                        else:
                            val.remove(v)
                            flatten_again = True

                if not val:
                    cur_diff['attributes'].pop(attr)
                    flatten_again = True

            if flatten_again:
                to_flatten.append(diff_hierarchy)
            if new_to_flatten:
                to_flatten.extend(new_to_flatten)

            if not cur_diff['attributes']:
                cur_diff.pop('attributes')
                cur_diff.pop('objects')

                to_flatten.append(diff_hierarchy[0:-1])

        return difference

    def _render_difference(self, difference):
        """ Generate string representation of difference data structure

        Args:
            difference (:obj:`dict`): representation of the semantic difference between two objects
        """
        msg = ''
        to_render = [[difference, 0, '']]
        while to_render:
            difference, indent, prefix = to_render.pop()

            msg += prefix

            if 'type' in difference:
                if indent:
                    msg += '\n' + ' ' * 2 * indent
                msg += difference['type']

            if 'attributes' in difference:
                if indent:
                    msg += '\n' + ' ' * 2 * indent
                msg += 'Objects ({}: "{}", {}: "{}") have different attribute values:'.format(
                    difference['objects'][0].__class__.__name__,
                    difference['objects'][0].serialize(), 
                    difference['objects'][1].__class__.__name__,
                    difference['objects'][1].serialize(),
                    )

                for attr_name in natsorted(difference['attributes'].keys(), alg=ns.IGNORECASE):
                    prefix = '\n{}`{}` are not equal:'.format(' ' * 2 * (indent + 1), attr_name)
                    if isinstance(difference['attributes'][attr_name], dict):
                        to_render.append([difference['attributes'][attr_name], indent + 2, prefix, ])

                    elif isinstance(difference['attributes'][attr_name], list):
                        new_to_render = []
                        new_to_msg = ''
                        for i_el, el_diff in enumerate(difference['attributes'][attr_name]):
                            if isinstance(el_diff, dict):
                                el_prefix = '\n{}element: {}: "{}" != element: {}: "{}"'.format(
                                    ' ' * 2 * (indent + 2), 
                                    el_diff['objects'][0].__class__.__name__,
                                    el_diff['objects'][0].serialize(), 
                                    el_diff['objects'][1].__class__.__name__,
                                    el_diff['objects'][1].serialize(),
                                    )
                                new_to_render.append([el_diff, indent + 3, el_prefix, ])
                            else:
                                new_to_msg += '\n' + ' ' * 2 * (indent + 2) + el_diff

                        if new_to_msg:
                            msg += prefix + new_to_msg
                            prefix = ''

                        if new_to_render:
                            new_to_render[0][2] = prefix + new_to_render[0][2]
                            new_to_render.reverse()
                            to_render.extend(new_to_render)
                    else:
                        msg += prefix + '\n' + ' ' * 2 * (indent + 2) + difference['attributes'][attr_name]

        return msg

    def get_primary_attribute(self):
        """ Get value of primary attribute

        Returns:
            :obj:`object`: value of primary attribute
        """
        if self.__class__.Meta.primary_attribute:
            return getattr(self, self.__class__.Meta.primary_attribute.name)

        return None

    def serialize(self):
        """ Get value of primary attribute

        Returns:
            :obj:`str`: value of primary attribute
        """
        return self.get_primary_attribute()

    @classmethod
    def deserialize(cls, value, objects):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if value in objects[cls]:
            return (objects[cls][value], None)

        attr = cls.Meta.primary_attribute
        return (None, InvalidAttribute(attr, ['No object with primary attribute value "{}"'.format(value)]))

    def get_related(self):
        """ Get all related objects

        Returns:
            :obj:`set` of `Model`: related objects
        """
        related_objs = list()
        objs_to_explore = [self]
        init_iter = True
        while objs_to_explore:
            obj = objs_to_explore.pop()
            if obj not in related_objs:
                if not init_iter:
                    related_objs.append(obj)
                init_iter = False

                cls = obj.__class__
                for attr_name, attr in chain(cls.Meta.attributes.items(), cls.Meta.related_attributes.items()):
                    if isinstance(attr, RelatedAttribute):
                        value = getattr(obj, attr_name)

                        if isinstance(value, list):
                            objs_to_explore.extend(value)
                        elif value is not None:
                            objs_to_explore.append(value)

        return related_objs

    def clean(self):
        """ Clean all of this `Model`'s attributes

        Returns:
            :obj:`InvalidObject` or None: `None` if the object is valid,
                otherwise return a list of errors as an instance of `InvalidObject`
        """
        errors = []

        for attr_name, attr in self.Meta.attributes.items():
            value = getattr(self, attr_name)
            clean_value, error = attr.clean(value)

            if error:
                errors.append(error)
            else:
                self.__setattr__(attr_name, clean_value)

        if errors:
            return InvalidObject(self, errors)
        return None

    def validate(self):
        """ Determine if the object is valid

        Returns:
            :obj:`InvalidObject` or None: `None` if the object is valid,
                otherwise return a list of errors as an instance of `InvalidObject`
        """
        errors = []

        # attributes
        for attr_name, attr in self.Meta.attributes.items():
            error = attr.validate(self, getattr(self, attr_name))
            if error:
                errors.append(error)

        # related attributes
        for attr_name, attr in self.Meta.related_attributes.items():
            if attr.related_name:
                error = attr.related_validate(self, getattr(self, attr.related_name))
                if error:
                    errors.append(error)

        if errors:
            return InvalidObject(self, errors)
        return None

    @classmethod
    def validate_unique(cls, objects):
        """ Validate attribute uniqueness

        Args:
            objects (:obj:`list` of `Model`): list of objects

        Returns:
            :obj:`InvalidModel` or `None`: list of invalid attributes and their errors
        """
        errors = []

        # validate uniqueness of individual attributes
        for attr_name, attr in cls.Meta.attributes.items():
            if attr.unique:
                vals = []
                for obj in objects:
                    vals.append(getattr(obj, attr_name))

                error = attr.validate_unique(objects, vals)
                if error:
                    errors.append(error)

        # validate uniqueness of combinations of attributes
        for unique_together in cls.Meta.unique_together:
            vals = set()
            rep_vals = set()
            for obj in objects:
                val = []
                for attr_name in unique_together:
                    attr_val = getattr(obj, attr_name)
                    if isinstance(attr_val, RelatedManager):
                        val.append(tuple(sorted((id(sub_val) for sub_val in attr_val))))
                    elif isinstance(attr_val, Model):
                        val.append(id(attr_val))
                    else:
                        val.append(attr_val)
                val = tuple(val)

                if val in vals:
                    rep_vals.add(val)
                else:
                    vals.add(val)

            if rep_vals:
                msg = 'Combinations of ({}) must be unique. The following combinations are repeated:'.format(
                    ', '.join(unique_together))
                for rep_val in rep_vals:
                    msg += '\n  {}'.format(', '.join((str(x) for x in rep_val)))
                attr = cls.Meta.attributes[list(unique_together)[0]]
                errors.append(InvalidAttribute(attr, [msg]))

        # return
        if errors:
            return InvalidModel(cls, errors)
        return None

    DEFAULT_MAX_DEPTH=2
    DEFAULT_INDENT=3
    def pprint(self, stream=None, max_depth=DEFAULT_MAX_DEPTH, indent=DEFAULT_INDENT):
        if stream is None:
            stream = sys.stdout
        print(self.pformat(max_depth=max_depth, indent=indent), file=stream)

    def pformat(self, max_depth=DEFAULT_MAX_DEPTH, indent=DEFAULT_INDENT):
        """ Return a human-readable string representation of this `Model`.

            Follows the graph of related `Model`'s up to a depth of `max_depth`. `Model`'s at depth
            `max_depth+1` are represented by '<class name>: ...', while deeper `Model`'s are not
            traversed or printed. Re-encountered Model's do not get printed, and are indicated by
            '<attribute name>: --'.
            Attributes that are related or iterable are indented.

            For example, we have::

                Model1 classname:       # Each model starts with its classname, followed by a list of
                attr1: value1           # attribute names & values.
                attr2: value2
                attr3:                  # Reference attributes can point to other Models; we indent these under the attribute name
                    Model2 classname:   # Reference attribute attr3 contains Model2;
                    ...                 # its attributes follow.
                attr4:
                    Model3 classname:   # An iteration over reference attributes is a list at constant indentation:
                    ...
                attr5:
                    Model2 classname: --    # Traversing the Model network may re-encounter a Model; they're listed with '--'
                attr6:
                    Model5 classname:
                    attr7:
                        Model5 classname: ...   # The size of the output is controlled with max_depth;
                                                # models encountered at depth = max_depth+1 are shown with '...'

        Args:
            max_depth (:obj:`int`, optional): the maximum depth to which related `Model`'s should be printed
            indent (:obj:`int`, optional): number of spaces to indent

        Returns:
            :obj:str: readable string representation of this `Model`
        """
        printed_objs = set()
        return indent_forest(self._tree_str(printed_objs, depth=0, max_depth=max_depth), indentation=indent)

    def _tree_str(self, printed_objs, depth, max_depth):
        """ Obtain a nested list of string representations of this Model.

            Follows the graph of related `Model`'s up to a depth of `max_depth`. Called recursively.

        Args:
            printed_objs (:obj:`set`): objects that have already been `_tree_str`'ed
            depth (:obj:`int`): the depth at which this `Model` is being `_tree_str`'ed
            max_depth (:obj:`int`): the maximum depth to which related `Model`'s should be printed

        Returns:
            :obj:`list` of `list`: a nested list of string representations of this Model

        Raises:
            :obj:`ValuerError`: if an attribute cannot be represented as a string, or a
            related attribute value is not `None`, a `Model`, or an Iterable
        """
        '''
        TODO: many possible improvements
            output to formattable text, most likely html
                in html, distinguish class names, attribute names, and values; link to previously
                printed Models; make deeper references collapsable
            could convert to YAML, and use YAML renderers
            take iterable of models instead of one
            take sets of attributes to print, or not print
            don't display empty attributes
        '''
        # get class
        cls = self.__class__

        # check depth
        if max_depth<depth:
            return ["{}: {}".format(cls.__name__, '...')]

        printed_objs.add(self)

        # get attribute names and their string values
        attrs = [(cls.__name__, '')]

        # first do the attributes in cls.Meta.attribute_order in that order, then do the rest
        all_attrs = cls.Meta.attributes.copy()
        all_attrs.update(cls.Meta.related_attributes)
        ordered_attrs = []
        for name in cls.Meta.attribute_order:
            ordered_attrs.append((name, all_attrs[name]))
        for name in all_attrs.keys():
            if name not in cls.Meta.attribute_order:
                ordered_attrs.append((name, all_attrs[name]))
        for name,attr in ordered_attrs:
            val = getattr(self, name)

            if isinstance(attr, RelatedAttribute):
                if val is None:
                    attrs.append((name, val))
                elif isinstance(val, Model):
                    if val in printed_objs:
                        attrs.append((name, '--'))
                    else:
                        attrs.append((name, ''))
                        attrs.append(val._tree_str(printed_objs, depth+1, max_depth))
                elif isinstance(val, (set, list, tuple)):
                    attrs.append((name, ''))
                    iter_attr = []
                    for v in val:
                        if not v in printed_objs:
                            iter_attr.append(v._tree_str(printed_objs, depth+1, max_depth))
                    attrs.extend(iter_attr)
                else:
                    raise ValueError("Related attribute '{}' has invalid value".format(name))

            elif isinstance(attr, Attribute):
                if val is None:
                    attrs.append((name, val))
                elif isinstance(val, (string_types, bool, integer_types, float, Enum)):
                    attrs.append((name, str(val)))
                elif hasattr(attr, 'serialize'):
                    attrs.append((name, attr.serialize(val)))
                else:
                    raise ValueError("Attribute '{}' has invalid value '{}'".format(name, str(val)))

            else:
                raise ValueError("Attribute '{}' is not an Attribute or RelatedAttribute".format(name))

        rv = []
        for item in attrs:
            if isinstance(item, tuple):
                name, val = item
                rv.append("{}: {}".format(name, val))
            else:
                rv.append(item)
        return rv

    def copy(self):
        """ Create a copy

        Returns:
            :obj:`Model`: model copy
        """

        # initialize copies of objects
        objects_and_copies = {}
        for obj in chain([self], self.get_related()):
            copy = obj.__class__()
            objects_and_copies[obj] = copy

        # copy attribute values
        for obj, copy in objects_and_copies.items():
            obj._copy_attributes(copy, objects_and_copies)

        # return copy
        return objects_and_copies[self]

    def _copy_attributes(self, other, objects_and_copies):
        """ Copy the attributes from `self` to its new copy, `other`

        Args:
            other (:obj:`Model`): object to copy attribute values to
            objects_and_copies (:obj:`dict` of `Model`: `Model`): dictionary of pairs of objects and their new copies

        Raises:
            :obj:`ValuerError`: if related attribute value is not `None`, a `Model`, or an Iterable,
                or if a non-related attribute is not an immutable
        """
        # get class
        cls = self.__class__

        # copy attributes
        for attr in cls.Meta.attributes.values():
            val = getattr(self, attr.name)

            if isinstance(attr, RelatedAttribute):
                if val is None:
                    copy_val = val
                elif isinstance(val, Model):
                    copy_val = objects_and_copies[val]
                elif isinstance(val, (set, list, tuple)):
                    copy_val = []
                    for v in val:
                        copy_val.append(objects_and_copies[v])
                else:
                    raise ValueError('Invalid related attribute value')
            else:
                if val is None:
                    copy_val = val
                elif isinstance(val, (string_types, bool, integer_types, float, Enum, )):
                    copy_val = copy.copy(val)
                else:
                    raise ValueError('Invalid attribute value')

            setattr(other, attr.name, copy_val)

    @classmethod
    def is_serializable(cls):
        """ Determine if the class (and its related classes) can be serialized

        Raises:
            :obj:`bool`: `True` if the class can be serialized
        """
        classes_to_check = [cls]
        checked_classes = []
        while classes_to_check:
            cls = classes_to_check.pop()
            if cls not in checked_classes:
                checked_classes.append(cls)

                if not cls.are_related_attributes_serializable():
                    return False

                for attr in cls.Meta.attributes.values():
                    if isinstance(attr, RelatedAttribute):
                        classes_to_check.append(attr.related_class)

                for attr in cls.Meta.related_attributes.values():
                    if isinstance(attr, RelatedAttribute):
                        classes_to_check.append(attr.primary_class)

        return True

    @classmethod
    def are_related_attributes_serializable(cls):
        """ Determine if the immediate related attributes of the class can be serialized

        Raises:
            :obj:`bool`: `True` if the related attributes can be serialized
        """
        for attr in cls.Meta.attributes.values():
            if isinstance(attr, RelatedAttribute):

                # setup related attributes on related classes
                if attr.name in cls.__dict__ and attr.related_name and \
                        isinstance(attr.related_class, type) and issubclass(attr.related_class, Model):
                    related_classes = chain([attr.related_class], get_subclasses(attr.related_class))
                    for related_class in related_classes:
                        # check that related class has primary attributes
                        if isinstance(attr, (OneToManyAttribute, ManyToManyAttribute)) and \
                                attr.__class__ is not OneToManyAttribute and \
                                attr.__class__ is not ManyToManyAttribute and \
                                'serialize' in attr.__class__.__dict__ and \
                                'deserialize' in attr.__class__.__dict__:
                            pass
                        elif not related_class.Meta.primary_attribute:
                            if related_class.Meta.tabular_orientation == TabularOrientation.inline:
                                warnings.warn('Primary class: {}: Related class {} must have a primary attribute'.format(
                                    attr.primary_class.__name__, related_class.__name__), SchemaWarning)
                            else:
                                return False
                        elif not related_class.Meta.primary_attribute.unique:
                            if related_class.Meta.tabular_orientation == TabularOrientation.inline:
                                warnings.warn('Primary attribute {} of related class {} must be unique'.format(
                                    related_class.Meta.primary_attribute.name, related_class.__name__), SchemaWarning)
                            else:
                                return False
        return True

    @classmethod
    def get_manager(cls):
        return cls.objects

class ModelSource(object):
    """ Represents the file, sheet, columns, and row where a :obj:`Model` instance was defined

    Attributes:
        path_name (:obj:`str`): pathname of source file for object
        sheet_name (:obj:`str`): name of spreadsheet containing source data for object
        attribute_seq (:obj:`list`): sequence of attribute names in source file; blank values
            indicate attributes that were ignored
        row (:obj:`int`): row number of object in its source file
    """

    def __init__(self, path_name, sheet_name, attribute_seq, row):
        """
        Args:
            path_name (:obj:`str`): pathname of source file for object
            sheet_name (:obj:`str`): name of spreadsheet containing source data for object
            attribute_seq (:obj:`list`): sequence of attribute names in source file; blank values
                indicate attributes that were ignored
            row (:obj:`int`): row number of object in its source file
        """
        self.path_name = path_name
        self.sheet_name = sheet_name
        self.attribute_seq = attribute_seq
        self.row = row


class Attribute(object):
    """ Model attribute

    Attributes:
        name (:obj:`str`): name
        init_value(:obj:`object`): initial value
        default (:obj:`object`): default value
        verbose_name (:obj:`str`): verbose name
        help (:obj:`str`): help string
        primary (:obj:`bool`): indicate if attribute is primary attribute
        unique (:obj:`bool`): indicate if attribute value must be unique
        unique_case_insensitive (:obj:`bool`): if true, conduct case-insensitive test of uniqueness
    """

    def __init__(self, init_value=None, default=None, verbose_name='', help='',
                 primary=False, unique=False, unique_case_insensitive=False):
        """
        Args:
            init_value(:obj:`object`, optional): initial value
            default (:obj:`object`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
            unique_case_insensitive (:obj:`bool`, optional): if true, conduct case-insensitive test of uniqueness
        """
        self.name = None
        self.init_value = init_value
        self.default = default
        self.verbose_name = verbose_name
        self.primary = primary
        self.unique = unique
        self.unique_case_insensitive = unique_case_insensitive

    def get_init_value(self, obj):
        """ Get initial value for attribute

        Args:
            obj (:obj:`Model`): object whose attribute is being initialized

        Returns:
            :obj:`object`: initial value
        """
        if self.init_value and hasattr(self.init_value, '__call__'):
            return self.init_value()

        return copy.copy(self.init_value)

    def get_default(self, obj):
        """ Get default value for attribute

        Args:
            obj (:obj:`Model`): object whose attribute is being initialized

        Returns:
            :obj:`object`: initial value
        """
        if self.default and hasattr(self.default, '__call__'):
            return self.default()

        return copy.copy(self.default)

    def set_value(self, obj, new_value):
        """ Set value of attribute of object

        Args:
            obj (:obj:`Model`): object
            new_value (:obj:`object`): new attribute value

        Returns:
            :obj:`object`: attribute value
        """
        return new_value

    def value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`object`): first value
            val2 (:obj:`object`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        return val1 == val2

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        return (value, None)

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, otherwise return a list of errors as an instance of `InvalidAttribute`
        """
        return None

    def validate_unique(self, objects, values):
        """ Determine if the attribute values are unique

        Args:
            objects (:obj:`list` of `Model`): list of `Model` objects
            values (:obj:`list`): list of values

        Returns:
           :obj:`InvalidAttribute` or None: None if values are unique, otherwise return a list of errors as an instance of `InvalidAttribute`
        """
        unq_vals = set()
        rep_vals = set()

        for val in values:
            if self.unique_case_insensitive and isinstance(val, string_types):
                val = val.lower()
            if val in unq_vals:
                rep_vals.add(val)
            else:
                unq_vals.add(val)

        if rep_vals:
            message = "{} values must be unique, but these values are repeated: {}".format(self.name,
                                                                                           ', '.join([quote(val) for val in rep_vals]))
            return InvalidAttribute(self, [message])

    def serialize(self, value):
        """ Serialize value

        Args:
            value (:obj:`object`): Python representation

        Returns:
            :obj:`bool`, `float`, `str`, or `None`: simple Python representation
        """
        return value

    def deserialize(self, value):
        """ Deserialize value

        Args:
            value (:obj:`object`): semantically equivalent representation

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        return self.clean(value)


class LiteralAttribute(Attribute):
    """ Base class for literal attributes (Boolean, enumeration, float, integer, string, etc.) """
    pass

class NumericAttribute(LiteralAttribute):
    """ Base class for numeric literal attributes (float, integer) """
    pass


class EnumAttribute(LiteralAttribute):
    """ Enumeration attribute

    Attributes:
        enum_class (:obj:`type`): subclass of `Enum`
    """

    def __init__(self, enum_class, default=None, verbose_name='', help='',
                 primary=False, unique=False, unique_case_insensitive=False):
        """
        Args:
            enum_class (:obj:`type`): subclass of `Enum`
            default (:obj:`object`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
            unique_case_insensitive (:obj:`bool`, optional): if true, conduct case-insensitive test of uniqueness

        Raises:
            :obj:`ValueError`: if `enum_class` is not an instance of `Enum` or if `default` is not an instance of `enum_class`
        """
        if not issubclass(enum_class, Enum):
            raise ValueError('`enum_class` must be an subclass of `Enum`')
        if default is not None and not isinstance(default, enum_class):
            raise ValueError('Default must be None or an instance of `enum_class`')

        super(EnumAttribute, self).__init__(default=default,
                                            verbose_name=verbose_name, help=help,
                                            primary=primary, unique=unique, unique_case_insensitive=unique_case_insensitive)

        self.enum_class = enum_class

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `Enum`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        error = None

        if isinstance(value, string_types):
            try:
                value = self.enum_class[value]
            except KeyError:
                error = 'Value "{}" is not convertible to an instance of {} which contains {}'.format(
                    value, self.enum_class.__name__, list(self.enum_class.__members__.keys()))

        elif isinstance(value, (integer_types, float)):
            try:
                value = self.enum_class(value)
            except ValueError:
                error = 'Value "{}" is not convertible to an instance of {}'.format(value,
                                                                                    self.enum_class.__name__)

        elif not isinstance(value, self.enum_class):
            error = "Value '{}' must be an instance of `{}` which contains {}".format(value,
                                                                                      self.enum_class.__name__, list(self.enum_class.__members__.keys()))

        if error:
            return (None, InvalidAttribute(self, [error]))
        else:
            return (value, None)

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(EnumAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if not isinstance(value, self.enum_class):
            errors.append("Value '{}' must be an instance of `{}` which contains {}".format(value,
                                                                                            self.enum_class.__name__, list(self.enum_class.__members__.keys())))

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize enumeration

        Args:
            value (:obj:`Enum`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """
        return value.name


class BooleanAttribute(LiteralAttribute):
    """ Boolean attribute

    Attributes:
        default (:obj:`bool`): default value
    """

    def __init__(self, default=False, verbose_name='', help='Enter a Boolean value'):
        """
        Args:
            default (:obj:`float`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string

        Raises:
            :obj:`ValueError`: if `default` is not a `bool`
        """
        if default is not None and not isinstance(default, bool):
            raise ValueError('`default` must be None or an instance of `bool`')

        super(BooleanAttribute, self).__init__(default=default,
                                               verbose_name=verbose_name, help=help,
                                               primary=False, unique=False, unique_case_insensitive=False)

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `bool`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        errors = []
        if isinstance(value, string_types):
            if value == '':
                value = None
            elif value in ['true', 'True', 'TRUE', '1']:
                value = True
            elif value in ['false', 'False', 'FALSE', '0']:
                value = False

        try:
            float_value = float(value)

            if isnan(float_value):
                value = None
            elif float_value == 0.:
                value = False
            elif float_value == 1.:
                value = True
        except ValueError:
            pass

        if (value is None) or isinstance(value, bool):
            return (value, None)
        return (None, InvalidAttribute(self, ['Value must be a `bool` or `None`']))

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(BooleanAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is not None and not isinstance(value, bool):
            errors.append('Value must be an instance of `bool` or `None`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize value

        Args:
            value (:obj:`bool`): Python representation

        Returns:
            :obj:`bool`: simple Python representation
        """
        return value


class FloatAttribute(NumericAttribute):
    """ Float attribute

    Attributes:
        default (:obj:`float`): default value
        min (:obj:`float`): minimum value
        max (:obj:`float`): maximum value
        nan (:obj:`bool`): if true, allow nan values
    """

    def __init__(self, min=float('nan'), max=float('nan'), nan=True,
                 default=float('nan'), verbose_name='', help='',
                 primary=False, unique=False):
        """
        Args:
            min (:obj:`float`, optional): minimum value
            max (:obj:`float`, optional): maximum value
            nan (:obj:`bool`, optional): if true, allow nan values
            default (:obj:`float`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique

        Raises:
            :obj:`ValueError`: if `max` is less than `min`
        """
        min = float(min)
        max = float(max)
        default = float(default)
        if not isnan(min) and not isnan(max) and max < min:
            raise ValueError('max must be at least min')

        super(FloatAttribute, self).__init__(default=default,
                                             verbose_name=verbose_name, help=help,
                                             primary=primary, unique=unique, unique_case_insensitive=False)

        self.min = min
        self.max = max
        self.nan = nan

    def value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`object`): first value
            val2 (:obj:`object`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        return val1 == val2 or (isnan(val1) and isnan(val2)) or abs((val1 - val2) / val1) < 1e-10

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `float`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if value is None or (isinstance(value, string_types) and value == ''):
            value = float('nan')

        try:
            value = float(value)
            return (value, None)
        except ValueError:
            return (None, InvalidAttribute(self, ['Value must be a `float`']))

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(FloatAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if isinstance(value, float):
            if not self.nan and isnan(value):
                errors.append('Value cannot be `nan`')

            if (not isnan(self.min)) and (not isnan(value)) and (value < self.min):
                errors.append('Value must be at least {:f}'.format(self.min))

            if (not isnan(self.max)) and (not isnan(value)) and (value > self.max):
                errors.append('Value must be at most {:f}'.format(self.max))
        else:
            errors.append('Value must be an instance of `float`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize float

        Args:
            value (:obj:`float`): Python representation

        Returns:
            :obj:`float`: simple Python representation
        """
        if isnan(value):
            return None
        return value


class IntegerAttribute(NumericAttribute):
    """ Interger attribute

    Attributes:
        default (:obj:`int`): default value
        min (:obj:`int`): minimum value
        max (:obj:`int`): maximum value
    """

    def __init__(self, min=None, max=None, default=None, verbose_name='', help='', primary=False, unique=False):
        """
        Args:
            min (:obj:`int`, optional): minimum value
            max (:obj:`int`, optional): maximum value
            default (:obj:`int`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique

        Raises:
            :obj:`ValueError`: if `max` is less than `min`
        """
        if min is not None:
            min = int(min)
        if max is not None:
            max = int(max)
        if default is not None:
            default = int(default)
        if min is not None and max is not None and max < min:
            raise ValueError('max must be at least min')

        super(IntegerAttribute, self).__init__(default=default,
                                               verbose_name=verbose_name, help=help,
                                               primary=primary, unique=unique, unique_case_insensitive=False)

        self.min = min
        self.max = max

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `int`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """

        if value is None or (isinstance(value, string_types) and value == ''):
            return (value, None, )

        try:
            if float(value) == int(float(value)):
                return (int(float(value)), None, )
        except ValueError:
            pass
        return (None, InvalidAttribute(self, ['Value must be an integer']), )

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, otherwise return list of
                errors as an instance of `InvalidAttribute`
        """
        errors = super(IntegerAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if isinstance(value, integer_types):
            if self.min is not None:
                if value is None:
                    errors.append('Value cannot be None')
                elif value < self.min:
                    errors.append('Value must be at least {:d}'.format(self.min))

            if self.max is not None:
                if value is None:
                    errors.append('Value cannot be None')
                elif value > self.max:
                    errors.append('Value must be at most {:d}'.format(self.max))
        elif value is not None:
            errors.append('Value must be an instance of `int` or `None`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize interger

        Args:
            value (:obj:`int`): Python representation

        Returns:
            :obj:`float`: simple Python representation
        """
        if value is None:
            return None
        return float(value)


class PositiveIntegerAttribute(IntegerAttribute):
    """ Positive interger attribute """

    def __init__(self, max=None, default=None, verbose_name='', help='', primary=False, unique=False):
        """
        Args:
            min (:obj:`int`, optional): minimum value
            max (:obj:`int`, optional): maximum value
            default (:obj:`int`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """
        super(PositiveIntegerAttribute, self).__init__(min=None, max=max, default=default,
                                                       verbose_name=verbose_name, help=help,
                                                       primary=primary, unique=unique)

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """

        error = super(PositiveIntegerAttribute, self).validate(obj, value)
        if error:
            errors = error.messages
        else:
            errors = []

        if (value is not None) and (float(value) <= 0):
            errors.append('Value must be positive')

        if errors:
            return InvalidAttribute(self, errors)
        return None


class StringAttribute(LiteralAttribute):
    """ String attribute

    Attributes:
        default (:obj:`str`, optional): default value
        min_length (:obj:`int`): minimum length
        max_length (:obj:`int`): maximum length
    """

    def __init__(self, min_length=0, max_length=255, default='', verbose_name='', help='',
                 primary=False, unique=False, unique_case_insensitive=False):
        """
        Args:
            min_length (:obj:`int`, optional): minimum length
            max_length (:obj:`int`, optional): maximum length
            default (:obj:`str`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
            unique_case_insensitive (:obj:`bool`, optional): if true, conduct case-insensitive test of uniqueness

        Raises:
            :obj:`ValueError`: if `min_length` is negative, `max_length` is less than `min_length`, or `default` is not a string
        """

        if not isinstance(min_length, integer_types) or min_length < 0:
            raise ValueError('min_length must be a non-negative integer')
        if (max_length is not None) and (not isinstance(max_length, integer_types) or max_length < 0):
            raise ValueError('max_length must be None or a non-negative integer')
        if not isinstance(default, string_types):
            raise ValueError('Default must be a string')

        super(StringAttribute, self).__init__(default=default,
                                              verbose_name=verbose_name, help=help,
                                              primary=primary, unique=unique, unique_case_insensitive=unique_case_insensitive)

        self.min_length = min_length
        self.max_length = max_length

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `str`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if value is None:
            value = ''
        elif not isinstance(value, string_types):
            value = str(value)
        return (value, None)

    def validate(self, obj, value):
        """ Determine if `value` is a valid value for this StringAttribute

        Args:
            obj (:obj:`Model`): class being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(StringAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if not isinstance(value, string_types):
            errors.append('Value must be an instance of `str`')
        else:
            if self.min_length and len(value) < self.min_length:
                errors.append('Value must be at least {:d} characters'.format(self.min_length))

            if self.max_length and len(value) > self.max_length:
                errors.append('Value must be less than {:d} characters'.format(self.max_length))

            if self.primary and (value == '' or value is None):
                errors.append('{} value for primary attribute cannot be empty'.format(
                    self.__class__.__name__))

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize string

        Args:
            value (:obj:`str`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """
        return value


class LongStringAttribute(StringAttribute):
    """ Long string attribute """

    def __init__(self, min_length=0, max_length=2**32 - 1, default='', verbose_name='', help='',
                 primary=False, unique=False, unique_case_insensitive=False):
        """
        Args:
            min_length (:obj:`int`, optional): minimum length
            max_length (:obj:`int`, optional): maximum length
            default (:obj:`str`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
            unique_case_insensitive (:obj:`bool`, optional): if true, conduct case-insensitive test of uniqueness
        """

        super(LongStringAttribute, self).__init__(min_length=min_length, max_length=max_length, default=default,
                                                  verbose_name=verbose_name, help=help,
                                                  primary=primary, unique=unique, unique_case_insensitive=unique_case_insensitive)


class RegexAttribute(StringAttribute):
    """ Regular expression attribute

    Attributes:
        pattern (:obj:`str`): regular expression pattern
        flags (:obj:`int`): regular expression flags
    """

    def __init__(self, pattern, flags=0, min_length=0, max_length=None, default='', verbose_name='', help='',
                 primary=False, unique=False):
        """
        Args:
            pattern (:obj:`str`): regular expression pattern
            flags (:obj:`int`, optional): regular expression flags
            min_length (:obj:`int`, optional): minimum length
            max_length (:obj:`int`, optional): maximum length
            default (:obj:`str`, optional): default value
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """

        unique_case_insensitive = bin(flags)[-2] == '1'
        super(RegexAttribute, self).__init__(min_length=min_length, max_length=max_length,
                                             default=default, verbose_name=verbose_name, help=help,
                                             primary=primary, unique=unique, unique_case_insensitive=unique_case_insensitive)
        self.pattern = pattern
        self.flags = flags

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`object`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(RegexAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if not re.match(self.pattern, value, flags=self.flags):
            errors.append("Value '{}' does not match pattern: {}".format(value, self.pattern))

        if errors:
            return InvalidAttribute(self, errors)
        return None


class SlugAttribute(RegexAttribute):
    """ Slug attribute to be used for string IDs """

    def __init__(self, verbose_name='', help=None, primary=True, unique=True):
        """
        Args:
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
        """
        if help is None:
            help = "Enter a unique string identifier that (1) starts with a letter, (2) is composed "
            "of letters, numbers and underscopes, and (3) is less than 64 characters long"

        super(SlugAttribute, self).__init__(pattern=r'^[a-z_][a-z0-9_]*$', flags=re.I,
                                            min_length=1, max_length=63,
                                            default='', verbose_name=verbose_name, help=help,
                                            primary=primary, unique=unique)


class UrlAttribute(RegexAttribute):
    """ URL attribute to be used for URLs """

    def __init__(self, min_length=0, verbose_name='URL', help='Enter a valid URL', primary=False, unique=False):
        """
        Args:
            min_length (:obj:`int`, optional): minimum length
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """
        core_pattern = '(?:http|ftp)s?://(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?(?:/?|[/?]\S+)'
        if min_length == 0:
            pattern = '^(|{})$'.format(core_pattern)
        else:
            pattern = '^{}$'.format(core_pattern)

        super(UrlAttribute, self).__init__(pattern=pattern,
                                           flags=re.I,
                                           min_length=min_length, max_length=2**16 - 1,
                                           default='', verbose_name=verbose_name, help=help,
                                           primary=primary, unique=unique)


class DateAttribute(LiteralAttribute):
    """ Date attribute

    Attributes:
        none (:obj:`bool`): if true, the attribute is invalid if its value is None
        default (:obj:`date`): default date
    """

    def __init__(self, none=True, default=None, verbose_name='', help='', primary=False, unique=False):
        """
        Args:
            none (:obj:`bool`, optional): if true, the attribute is invalid if its value is None
            default (:obj:`date`, optional): default date
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """
        super(DateAttribute, self).__init__(default=default,
                                            verbose_name=verbose_name, help=help,
                                            primary=primary, unique=unique)
        self.none = none

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple`: (`date`, `None`), or (`None`, `InvalidAttribute`) reporting error
        """
        if value is None:
            return (value, None)

        if isinstance(value, date):
            return (value, None)

        if isinstance(value, datetime):
            if value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0:
                return (value.date(), None)
            else:
                return (None, InvalidAttribute(self, ['Time must be 0:0:0.0']))

        if isinstance(value, string_types):
            try:
                datetime_value = dateutil.parser.parse(value)
                if datetime_value.hour == 0 and datetime_value.minute == 0 and datetime_value.second == 0 and datetime_value.microsecond == 0:
                    return (datetime_value.date(), None)
                else:
                    return (None, InvalidAttribute(self, ['Time must be 0:0:0.0']))
            except ValueError:
                return (None, InvalidAttribute(self, ['String must be a valid date']))

        try:
            float_value = float(value)
            int_value = int(float_value)
            if float_value == int_value:
                return (date.fromordinal(int_value + date(1900, 1, 1).toordinal() - 1), None)
        except ValueError:
            pass

        return (None, 'Value must be an instance of `date`')

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`date`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(DateAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if not self.none:
                errors.append('Value cannot be `None`')
        elif isinstance(value, date):
            if value.year < 1900 or value.year > 10000:
                errors.append('Year must be between 1900 and 9999')
        else:
            errors.append('Value must be an instance of `date`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize string

        Args:
            value (:obj:`date`): Python representation

        Returns:
            :obj:`float`: simple Python representation
        """
        return value.toordinal() - date(1900, 1, 1).toordinal() + 1.


class TimeAttribute(LiteralAttribute):
    """ Time attribute

    Attributes:
        none (:obj:`bool`): if true, the attribute is invalid if its value is None
        default (:obj:`time`): defaul time
    """

    def __init__(self, none=True, default=None, verbose_name='', help='', primary=False, unique=False):
        """
        Args:
            none (:obj:`bool`, optional): if true, the attribute is invalid if its value is None
            default (:obj:`time`, optional): default time
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """
        super(TimeAttribute, self).__init__(default=default,
                                            verbose_name=verbose_name, help=help,
                                            primary=primary, unique=unique)
        self.none = none

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `time`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if value is None:
            return (value, None)

        if isinstance(value, time):
            return (value, None)

        if isinstance(value, string_types):
            if re.match('^\d{1,2}:\d{1,2}(:\d{1,2})*$', value):
                try:
                    datetime_value = dateutil.parser.parse(value)
                    return (datetime_value.time(), None)
                except ValueError:
                    return (None, InvalidAttribute(self, ['String must be a valid time']))
            else:
                return (None, InvalidAttribute(self, ['String must be a valid time']))

        try:
            int_value = round(float(value) * 24 * 60 * 60)
            if int_value < 0 or int_value > 24 * 60 * 60 - 1:
                return (None, InvalidAttribute(self, ['Number must be a valid time']))

            hour = int(int_value / (60. * 60.))
            minutes = int((int_value - hour * 60. * 60.) / 60.)
            seconds = int(int_value % 60)
            return (time(hour, minutes, seconds), None)
        except ValueError:
            pass

        return (None, 'Value must be an instance of `time`')

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`time`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(TimeAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if not self.none:
                errors.append('Value cannot be `None`')
        elif isinstance(value, time):
            if value.microsecond != 0:
                errors.append('Microsecond must be 0')
        else:
            errors.append('Value must be an instance of `time`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize string

        Args:
            value (:obj:`time`): Python representation

        Returns:
            :obj:`float`: simple Python representation
        """
        return (value.hour * 60. * 60. + value.minute * 60. + value.second) / (24. * 60. * 60.)


class DateTimeAttribute(LiteralAttribute):
    """ Datetime attribute

    Attributes:
        none (:obj:`bool`): if true, the attribute is invalid if its value is None
        default (:obj:`datetime`): default datetime
    """

    def __init__(self, none=True, default=None, verbose_name='', help='', primary=False, unique=False):
        """
        Args:
            none (:obj:`bool`, optional): if true, the attribute is invalid if its value is None
            default (:obj:`datetime`, optional): default datetime
            verbose_name (:obj:`str`, optional): verbose name
            help (:obj:`str`, optional): help string
            primary (:obj:`bool`, optional): indicate if attribute is primary attribute
            unique (:obj:`bool`, optional): indicate if attribute value must be unique
        """
        super(DateTimeAttribute, self).__init__(default=default,
                                                verbose_name=verbose_name, help=help,
                                                primary=primary, unique=unique)
        self.none = none

    def clean(self, value):
        """ Convert attribute value into the appropriate type

        Args:
            value (:obj:`object`): value of attribute to clean

        Returns:
            :obj:`tuple` of `datetime`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if value is None:
            return (value, None)

        if isinstance(value, datetime):
            return (value, None)

        if isinstance(value, date):
            return (datetime.combine(value, time(0, 0, 0, 0)), None)

        if isinstance(value, string_types):
            try:
                return (dateutil.parser.parse(value), None)
            except ValueError:
                return (None, InvalidAttribute(self, ['String must be a valid datetime']))

        try:
            float_value = float(value)
            date_int_value = int(float_value)
            time_int_value = round((float_value % 1) * 24 * 60 * 60)

            date_value = date.fromordinal(date_int_value + date(1900, 1, 1).toordinal() - 1)

            if time_int_value < 0 or time_int_value > 24 * 60 * 60 - 1:
                return (None, InvalidAttribute(self, ['Number must be a valid datetime']))
            hour = int(time_int_value / (60. * 60.))
            minutes = int((time_int_value - hour * 60. * 60.) / 60.)
            seconds = int(time_int_value % 60)
            time_value = time(hour, minutes, seconds)

            return (datetime.combine(date_value, time_value), None)
        except ValueError:
            pass

        return (None, 'Value must be an instance of `datetime`')

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`datetime`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(DateTimeAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if not self.none:
                errors.append('Value cannot be `None`')
        elif isinstance(value, datetime):
            if value.year < 1900 or value.year > 10000:
                errors.append('Year must be between 1900 and 9999')
            if value.microsecond != 0:
                errors.append('Microsecond must be 0')
        else:
            errors.append('Value must be an instance of `date`')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def serialize(self, value):
        """ Serialize string

        Args:
            value (:obj:`datetime`): Python representation

        Returns:
            :obj:`float`: simple Python representation
        """
        date_value = value.date()
        time_value = value.time()

        return date_value.toordinal() - date(1900, 1, 1).toordinal() + 1 \
            + (time_value.hour * 60. * 60. + time_value.minute * 60. + time_value.second) / (24. * 60. * 60.)


class RelatedAttribute(Attribute):
    """ Attribute which represents relationships with other objects

    Attributes:
        primary_class (:obj:`class`): parent class
        related_class (:obj:`class`): related class
        related_name (:obj:`str`): name of related attribute on `related_class`
        verbose_related_name (:obj:`str`): verbose related name
        related_init_value (:obj:`object`): initial value of related attribute
        related_default (:obj:`object`): default value of related attribute
        min_related (:obj:`int`): minimum number of related objects in the forward direction
        max_related (:obj:`int`): maximum number of related objects in the forward direction
        min_related_rev (:obj:`int`): minimum number of related objects in the reverse direction
        max_related_rev (:obj:`int`): maximum number of related objects in the reverse direction
    """

    def __init__(self, related_class, related_name='',
                 init_value=None, default=None, related_init_value=None, related_default=None,
                 min_related=0, max_related=float('inf'), min_related_rev=0, max_related_rev=float('inf'),
                 verbose_name='', verbose_related_name='', help=''):
        """
        Args:
            related_class (:obj:`class`): related class
            related_name (:obj:`str`, optional): name of related attribute on `related_class`
            init_value (:obj:`object`, optional): initial value
            default (:obj:`object`, optional): default value
            related_init_value (:obj:`object`, optional): related initial value
            related_default (:obj:`object`, optional): related default value
            min_related (:obj:`int`, optional): minimum number of related objects in the forward direction
            max_related (:obj:`int`, optional): maximum number of related objects in the forward direction
            min_related_rev (:obj:`int`, optional): minimum number of related objects in the reverse direction
            max_related_rev (:obj:`int`, optional): maximum number of related objects in the reverse direction
            verbose_name (:obj:`str`, optional): verbose name
            verbose_related_name (:obj:`str`, optional): verbose related name
            help (:obj:`str`, optional): help string

        Raises:
            :obj:`ValueError`: If default or related_default is not None or a callable or default and related_default are both callables
        """

        if default and not hasattr(default, '__call__'):
            raise ValueError('Default must be None or a callable')

        if related_default and not hasattr(related_default, '__call__'):
            raise ValueError('Related default must be None or a callable')

        if default and related_default:
            raise ValueError('Default and related_default cannot both be used')

        if not verbose_related_name:
            verbose_related_name = sentencecase(related_name)

        super(RelatedAttribute, self).__init__(init_value=init_value, default=default, verbose_name=verbose_name, help=help,
                                               primary=False, unique=False, unique_case_insensitive=False)
        self.primary_class = None
        self.related_class = related_class
        self.related_name = related_name
        self.verbose_related_name = verbose_related_name
        self.related_init_value = related_init_value
        self.related_default = related_default
        self.min_related = min_related
        self.max_related = max_related
        self.min_related_rev = min_related_rev
        self.max_related_rev = max_related_rev

    def get_related_init_value(self, obj):
        """ Get initial related value for attribute

        Args:
            obj (:obj:`object`): object whose attribute is being initialized

        Returns:
            value (:obj:`object`): initial value

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        return copy.copy(self.related_init_value)

    def get_related_default(self, obj):
        """ Get default related value for attribute

        Args:
            obj (:obj:`Model`): object whose attribute is being initialized

        Returns:
            :obj:`object`: initial value
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        if self.related_default and hasattr(self.related_default, '__call__'):
            return self.related_default()

        return copy.copy(self.related_default)

    def set_related_value(self, obj, new_values):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_values (:obj:`object`): value of the attribute

        Returns:
            :obj:`object`: value of the attribute

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')
        return new_values

    def related_validate(self, obj, value):
        """ Determine if `value` is a valid value of the related attribute

        Args:
            obj (:obj:`Model`): object to validate
            value (:obj:`list`): value to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        return None

    def deserialize(self, value, objects):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        return (value, None)


class OneToOneAttribute(RelatedAttribute):
    """ Represents a one-to-one relationship between two types of objects. """

    def __init__(self, related_class, related_name='',
                 default=None, related_default=None,
                 min_related=0, min_related_rev=0,
                 verbose_name='', verbose_related_name='', help=''):
        """
        Args:
            related_class (:obj:`class`): related class
            related_name (:obj:`str`, optional): name of related attribute on `related_class`
            default (:obj:`callable`, optional): callable which returns default value
            related_default (:obj:`callable`, optional): callable which returns default related value
            min_related (:obj:`int`, optional): minimum number of related objects in the forward direction
            min_related_rev (:obj:`int`, optional): minimum number of related objects in the reverse direction
            verbose_name (:obj:`str`, optional): verbose name
            verbose_related_name (:obj:`str`, optional): verbose related name
            help (:obj:`str`, optional): help string

        Raises:
            :obj:`ValueError`: If default is not `None` or a callable
        """
        super(OneToOneAttribute, self).__init__(related_class, related_name=related_name,
                                                init_value=None, default=default,
                                                related_init_value=None, related_default=related_default,
                                                min_related=min_related, max_related=1,
                                                min_related_rev=min_related_rev, max_related_rev=1,
                                                verbose_name=verbose_name, help=help, verbose_related_name=verbose_related_name)

    def set_value(self, obj, new_value):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_value (:obj:`Model`): new attribute value

        Returns:
            :obj:`Model`: new attribute value

        Raises:
            :obj:`ValueError`: if related attribute of `new_value` is not `None`
        """
        cur_value = getattr(obj, self.name)
        if cur_value is new_value:
            return new_value

        if new_value and getattr(new_value, self.related_name):
            raise ValueError('Related attribute of `new_value` must be `None`')

        if self.related_name:
            if cur_value:
                cur_value.__setattr__(self.related_name, None, propagate=False)

            if new_value:
                new_value.__setattr__(self.related_name, obj, propagate=False)

        return new_value

    def set_related_value(self, obj, new_value):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_value (:obj:`Model`): value of the attribute

        Returns:
            :obj:`Model`: value of the attribute

        Raises:
            :obj:`ValueError`: if related property is not defined or the attribute of `new_value` is not `None`
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        cur_value = getattr(obj, self.related_name)
        if cur_value is new_value:
            return new_value

        if new_value and getattr(new_value, self.name):
            raise ValueError('Attribute of `new_value` must be `None`')

        if cur_value:
            cur_value.__setattr__(self.name, None, propagate=False)

        if new_value:
            new_value.__setattr__(self.name, obj, propagate=False)

        return new_value

    def value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`Model`): first value
            val2 (:obj:`Model`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if val1.__class__ is not val2.__class__:
            return False
        if val1 is None:
            return True
        return val1.eq_attributes(val2)

    def related_value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`Model`): first value
            val2 (:obj:`Model`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if val1.__class__ is not val2.__class__:
            return False
        if val1 is None:
            return True
        return val1.eq_attributes(val2)

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`Model`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(OneToOneAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if self.min_related == 1:
                errors.append('Value cannot be `None`')
        elif not isinstance(value, self.related_class):
            errors.append('Value must be an instance of "{:s}" or `None`'.format(self.related_class.__name__))
        elif self.related_name:
            if obj is not getattr(value, self.related_name):
                errors.append('Object must be related value')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def related_validate(self, obj, value):
        """ Determine if `value` is a valid value of the related attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`list` of `Model`): value to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(OneToOneAttribute, self).related_validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if self.min_related_rev == 1:
                errors.append('Value cannot be `None`')
        elif value and self.related_name:
            if not isinstance(value, self.primary_class):
                errors.append('Related value must be an instance of "{:s}"'.format(self.primary_class.__name__))
            elif getattr(value, self.name) is not obj:
                errors.append('Object must be related value')

        if errors:
            return InvalidAttribute(self, errors, related=True)
        return None

    def serialize(self, value):
        """ Serialize related object

        Args:
            value (:obj:`Model`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """
        if value is None:
            return ''

        primary_attr = value.__class__.Meta.primary_attribute
        return primary_attr.serialize(getattr(value, primary_attr.name))

    def deserialize(self, value, objects):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if not value:
            return (None, None)

        related_objs = []
        related_classes = chain([self.related_class], get_subclasses(self.related_class))
        for related_class in related_classes:
            if issubclass(related_class, Model) and value in objects[related_class]:
                related_objs.append(objects[related_class][value])

        if len(related_objs) == 0:
            primary_attr = self.related_class.Meta.primary_attribute
            return (None, InvalidAttribute(self, ['Unable to find {} with {}={}'.format(
                self.related_class.__name__, primary_attr.name, quote(value))]))

        if len(related_objs) == 1:
            return (related_objs[0], None)

        return (None, InvalidAttribute(self, ['Multiple matching objects with primary attribute = {}'.format(value)]))


class ManyToOneAttribute(RelatedAttribute):
    """ Represents a many-to-one relationship between two types of objects. This is analagous to a foreign key relationship in a database. """

    def __init__(self, related_class, related_name='',
                 default=None, related_default=list(),
                 min_related=0, min_related_rev=0, max_related_rev=float('inf'),
                 verbose_name='', verbose_related_name='', help=''):
        """
        Args:
            related_class (:obj:`class`): related class
            related_name (:obj:`str`, optional): name of related attribute on `related_class`
            default (:obj:`callable`, optional): callable which returns the default value
            related_default (:obj:`callable`, optional): callable which returns the default related value
            min_related (:obj:`int`, optional): minimum number of related objects in the forward direction
            min_related_rev (:obj:`int`, optional): minimum number of related objects in the reverse direction
            max_related_rev (:obj:`int`, optional): maximum number of related objects in the reverse direction
            verbose_name (:obj:`str`, optional): verbose name
            verbose_related_name (:obj:`str`, optional): verbose related name
            help (:obj:`str`, optional): help string
        """
        super(ManyToOneAttribute, self).__init__(related_class, related_name=related_name,
                                                 init_value=None, default=default,
                                                 related_init_value=ManyToOneRelatedManager, related_default=related_default,
                                                 min_related=min_related, max_related=1, min_related_rev=min_related_rev, max_related_rev=max_related_rev,
                                                 verbose_name=verbose_name, help=help, verbose_related_name=verbose_related_name)

    def get_related_init_value(self, obj):
        """ Get initial related value for attribute

        Args:
            obj (:obj:`object`): object whose attribute is being initialized

        Returns:
            value (:obj:`object`): initial value

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is undefined')

        return ManyToOneRelatedManager(obj, self)

    def set_value(self, obj, new_value):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_value (:obj:`Model`): new attribute value

        Returns:
            :obj:`Model`: new attribute value
        """
        cur_value = getattr(obj, self.name)
        if cur_value is new_value:
            return new_value

        if self.related_name:
            if cur_value:
                cur_related = getattr(cur_value, self.related_name)
                cur_related.remove(obj, propagate=False)

            if new_value:
                new_related = getattr(new_value, self.related_name)
                new_related.append(obj, propagate=False)

        return new_value

    def set_related_value(self, obj, new_values):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_values (:obj:`list`): value of the attribute

        Returns:
            :obj:`list`: value of the attribute

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        new_values_copy = list(new_values)

        cur_values = getattr(obj, self.related_name)
        cur_values.clear()
        cur_values.extend(new_values_copy)

        return cur_values

    def value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`Model`): first value
            val2 (:obj:`Model`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if val1.__class__ is not val2.__class__:
            return False
        if val1 is None:
            return True
        return val1.eq_attributes(val2)

    def related_value_equal(self, vals1, vals2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`list`): first value
            val2 (:obj:`list`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if vals1.__class__ != vals2.__class__:
            return False

        for v1 in vals1:
            match = False
            for v2 in vals2:
                if v1.eq_attributes(v2):
                    match = True
                    break
            if not match:
                return False

        return True

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`Model`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(ManyToOneAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if value is None:
            if self.min_related == 1:
                errors.append('Value cannot be `None`')
        elif not isinstance(value, self.related_class):
            errors.append('Value must be an instance of "{:s}" or `None`'.format(self.related_class.__name__))
        elif self.related_name:
            related_value = getattr(value, self.related_name)
            if not isinstance(related_value, list):
                errors.append('Related value must be a list')
            if obj not in related_value:
                errors.append('Object must be in related values')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def related_validate(self, obj, value):
        """ Determine if `value` is a valid value of the related attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`list` of `Model`): value to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(ManyToOneAttribute, self).related_validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if self.related_name:
            if not isinstance(value, list):
                errors.append('Related value must be a list')
            elif len(value) < self.min_related_rev:
                errors.append('There must be at least {} related values'.format(self.min_related_rev))
            elif len(value) > self.max_related_rev:
                errors.append('There cannot be more than {} related values'.format(self.max_related_rev))
            else:
                for v in value:
                    if not isinstance(v, self.primary_class):
                        errors.append('Related value must be an instance of "{:s}"'.format(self.primary_class.__name__))
                    elif getattr(v, self.name) is not obj:
                        errors.append('Object must be related value')

        if errors:
            return InvalidAttribute(self, errors, related=True)
        return None

    def serialize(self, value):
        """ Serialize related object

        Args:
            value (:obj:`Model`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """
        if value is None:
            return ''

        primary_attr = value.__class__.Meta.primary_attribute
        return primary_attr.serialize(getattr(value, primary_attr.name))

    def deserialize(self, value, objects):
        """ Deserialize value

        Args:
            value (:obj:`str`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if not value:
            return (None, None)

        related_objs = []
        related_classes = chain([self.related_class], get_subclasses(self.related_class))
        for related_class in related_classes:
            if issubclass(related_class, Model) and value in objects[related_class]:
                related_objs.append(objects[related_class][value])

        if len(related_objs) == 0:
            primary_attr = self.related_class.Meta.primary_attribute
            return (None, InvalidAttribute(self, ['Unable to find {} with {}={}'.format(
                self.related_class.__name__, primary_attr.name, quote(value))]))

        if len(related_objs) == 1:
            return (related_objs[0], None)

        return (None, InvalidAttribute(self, ['Multiple matching objects with primary attribute = {}'.format(value)]))


class OneToManyAttribute(RelatedAttribute):
    """ Represents a one-to-many relationship between two types of objects. This is analagous to a foreign key relationship in a database. """

    def __init__(self, related_class, related_name='',  default=list(), related_default=None,
                 min_related=0, max_related=float('inf'), min_related_rev=0,
                 verbose_name='', verbose_related_name='', help=''):
        """
        Args:
            related_class (:obj:`class`): related class
            related_name (:obj:`str`, optional): name of related attribute on `related_class`
            default (:obj:`callable`, optional): function which returns the default value
            related_default (:obj:`callable`, optional): function which returns the default related value
            min_related (:obj:`int`, optional): minimum number of related objects in the forward direction
            max_related (:obj:`int`, optional): maximum number of related objects in the forward direction
            min_related_rev (:obj:`int`, optional): minimum number of related objects in the reverse direction
            verbose_name (:obj:`str`, optional): verbose name
            verbose_related_name (:obj:`str`, optional): verbose related name
            help (:obj:`str`, optional): help string
        """
        super(OneToManyAttribute, self).__init__(related_class, related_name=related_name,
                                                 init_value=OneToManyRelatedManager, default=default,
                                                 related_init_value=None, related_default=related_default,
                                                 min_related=min_related, max_related=max_related, min_related_rev=min_related_rev, max_related_rev=1,
                                                 verbose_name=verbose_name, help=help, verbose_related_name=verbose_related_name)

    def get_init_value(self, obj):
        """ Get initial value for attribute

        Args:
            obj (:obj:`Model`): object whose attribute is being initialized

        Returns:
            :obj:`object`: initial value
        """
        return OneToManyRelatedManager(obj, self)

    def set_value(self, obj, new_values):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_values (:obj:`list`): value of the attribute

        Returns:
            :obj:`list`: value of the attribute
        """
        new_values_copy = list(new_values)

        cur_values = getattr(obj, self.name)
        cur_values.clear()
        cur_values.extend(new_values_copy)

        return cur_values

    def value_equal(self, vals1, vals2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`list`): first value
            val2 (:obj:`list`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if vals1.__class__ != vals2.__class__:
            return False

        for v1 in vals1:
            match = False
            for v2 in vals2:
                if v1.eq_attributes(v2):
                    match = True
                    break
            if not match:
                return False

        return True

    def related_value_equal(self, val1, val2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`Model`): first value
            val2 (:obj:`Model`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if val1.__class__ is not val2.__class__:
            return False
        if val1 is None:
            return True
        return val1.eq_attributes(val2)

    def set_related_value(self, obj, new_value):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_value (:obj:`Model`): new attribute value

        Returns:
            :obj:`Model`: new attribute value

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        cur_value = getattr(obj, self.related_name)
        if cur_value is new_value:
            return new_value

        if cur_value:
            cur_related = getattr(cur_value, self.name)
            cur_related.remove(obj, propagate=False)

        if new_value:
            new_related = getattr(new_value, self.name)
            new_related.append(obj, propagate=False)

        return new_value

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`list` of `Model`): value to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(OneToManyAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if not isinstance(value, list):
            errors.append('Related value must be a list')
        elif len(value) < self.min_related:
            errors.append('There must be at least {} related values'.format(self.min_related))
        elif len(value) > self.max_related:
            errors.append('There must be no more than {} related values'.format(self.max_related))
        else:
            for v in value:
                if not isinstance(v, self.related_class):
                    errors.append('Value must be an instance of "{:s}"'.format(self.related_class.__name__))
                elif self.related_name and getattr(v, self.related_name) is not obj:
                    errors.append('Object must be related value')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def related_validate(self, obj, value):
        """ Determine if `value` is a valid value of the related attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`Model`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(OneToManyAttribute, self).related_validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if self.related_name:
            if value is None:
                if self.min_related_rev == 1:
                    errors.append('Value cannot be `None`')
            elif not isinstance(value, self.primary_class):
                errors.append('Value must be an instance of "{:s}" or `None`'.format(self.primary_class.__name__))
            else:
                related_value = getattr(value, self.name)
                if not isinstance(related_value, list):
                    errors.append('Related value must be a list')
                if obj not in related_value:
                    errors.append('Object must be in related values')

        if errors:
            return InvalidAttribute(self, errors, related=True)
        return None

    def serialize(self, value):
        """ Serialize related object

        Args:
            value (:obj:`list` of `Model`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """

        serialized_vals = []
        for v in value:
            primary_attr = v.__class__.Meta.primary_attribute
            serialized_vals.append(primary_attr.serialize(getattr(v, primary_attr.name)))

        serialized_vals.sort(key=natsort_keygen(alg=ns.IGNORECASE))
        return ', '.join(serialized_vals)

    def deserialize(self, values, objects):
        """ Deserialize value

        Args:
            values (:obj:`object`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if not values:
            return (list(), None)

        deserialized_values = list()
        errors = []
        for value in values.split(','):
            value = value.strip()

            related_objs = []
            related_classes = chain([self.related_class], get_subclasses(self.related_class))
            for related_class in related_classes:
                if issubclass(related_class, Model) and related_class in objects and value in objects[related_class]:
                    related_objs.append(objects[related_class][value])

            if len(related_objs) == 1:
                deserialized_values.append(related_objs[0])
            elif len(related_objs) == 0:
                errors.append('Unable to find {} with {}={}'.format(
                    self.related_class.__name__, self.related_class.Meta.primary_attribute.name, quote(value)))
            else:
                errors.append('Multiple matching objects with primary attribute = {}'.format(value))

        if errors:
            return (None, InvalidAttribute(self, errors))
        return (deserialized_values, None)


class ManyToManyAttribute(RelatedAttribute):
    """ Represents a many-to-many relationship between two types of objects. """

    def __init__(self, related_class, related_name='', default=list(), related_default=list(),
                 min_related=0, max_related=float('inf'), min_related_rev=0, max_related_rev=float('inf'),
                 verbose_name='', verbose_related_name='', help=''):
        """
        Args:
            related_class (:obj:`class`): related class
            related_name (:obj:`str`, optional): name of related attribute on `related_class`
            default (:obj:`callable`, optional): function which returns the default values
            related_default (:obj:`callable`, optional): function which returns the default related values
            min_related (:obj:`int`, optional): minimum number of related objects in the forward direction
            max_related (:obj:`int`, optional): maximum number of related objects in the forward direction
            min_related_rev (:obj:`int`, optional): minimum number of related objects in the reverse direction
            max_related_rev (:obj:`int`, optional): maximum number of related objects in the reverse direction
            verbose_name (:obj:`str`, optional): verbose name
            verbose_related_name (:obj:`str`, optional): verbose related name
            help (:obj:`str`, optional): help string
        """
        super(ManyToManyAttribute, self).__init__(related_class, related_name=related_name,
                                                  init_value=ManyToManyRelatedManager, default=default,
                                                  related_init_value=ManyToManyRelatedManager, related_default=related_default,
                                                  min_related=min_related, max_related=max_related, min_related_rev=min_related_rev, max_related_rev=max_related_rev,
                                                  verbose_name=verbose_name, help=help, verbose_related_name=verbose_related_name)

    def get_init_value(self, obj):
        """ Get initial value for attribute

        Args:
            obj (:obj:`Model`): object whose attribute is being initialized

        Returns:
            :obj:`object`: initial value
        """
        return ManyToManyRelatedManager(obj, self, related=False)

    def get_related_init_value(self, obj):
        """ Get initial related value for attribute

        Args:
            obj (:obj:`object`): object whose attribute is being initialized

        Returns:
            value (:obj:`object`): initial value

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')
        return ManyToManyRelatedManager(obj, self, related=True)

    def set_value(self, obj, new_values):
        """ Get value of attribute of object

        Args:
            obj (:obj:`Model`): object
            new_values (:obj:`list`): new attribute value

        Returns:
            :obj:`list`: new attribute value
        """
        new_values_copy = list(new_values)

        cur_values = getattr(obj, self.name)
        cur_values.clear()
        cur_values.extend(new_values_copy)

        return cur_values

    def set_related_value(self, obj, new_values):
        """ Update the values of the related attributes of the attribute

        Args:
            obj (:obj:`object`): object whose attribute should be set
            new_values (:obj:`list`): value of the attribute

        Returns:
            :obj:`list`: value of the attribute

        Raises:
            :obj:`ValueError`: if related property is not defined
        """
        if not self.related_name:
            raise ValueError('Related property is not defined')

        new_values_copy = list(new_values)

        cur_values = getattr(obj, self.related_name)
        cur_values.clear()
        cur_values.extend(new_values_copy)

        return cur_values

    def value_equal(self, vals1, vals2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`list`): first value
            val2 (:obj:`list`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if vals1.__class__ != vals2.__class__:
            return False

        for v1 in vals1:
            match = False
            for v2 in vals2:
                if v1.eq_attributes(v2):
                    match = True
                    break
            if not match:
                return False

        return True

    def related_value_equal(self, vals1, vals2):
        """ Determine if attribute values are equal

        Args:
            val1 (:obj:`list`): first value
            val2 (:obj:`list`): second value

        Returns:
            :obj:`bool`: True if attribute values are equal
        """
        if vals1.__class__ != vals2.__class__:
            return False

        for v1 in vals1:
            match = False
            for v2 in vals2:
                if v1.eq_attributes(v2):
                    match = True
                    break
            if not match:
                return False

        return True

    def validate(self, obj, value):
        """ Determine if `value` is a valid value of the attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`list` of `Model`): value of attribute to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(ManyToManyAttribute, self).validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if not isinstance(value, list):
            errors.append('Value must be a `list`')
        elif len(value) < self.min_related:
            errors.append('There must be at least {} related values'.format(self.min_related))
        elif len(value) > self.max_related:
            errors.append('There cannot be more than {} related values'.format(self.max_related))
        else:
            for v in value:
                if not isinstance(v, self.related_class):
                    errors.append('Value must be a `list` of "{:s}"'.format(self.related_class.__name__))

                if self.related_name:
                    related_v = getattr(v, self.related_name)
                    if not isinstance(related_v, list):
                        errors.append('Related value must be a list')
                    if obj not in related_v:
                        errors.append('Object must be in related values')

        if errors:
            return InvalidAttribute(self, errors)
        return None

    def related_validate(self, obj, value):
        """ Determine if `value` is a valid value of the related attribute

        Args:
            obj (:obj:`Model`): object being validated
            value (:obj:`list` of `Model`): value to validate

        Returns:
            :obj:`InvalidAttribute` or None: None if attribute is valid, other return list of errors as an instance of `InvalidAttribute`
        """
        errors = super(ManyToManyAttribute, self).related_validate(obj, value)
        if errors:
            errors = errors.messages
        else:
            errors = []

        if self.related_name:
            if not isinstance(value, list):
                errors.append('Related value must be a list')
            elif len(value) < self.min_related_rev:
                errors.append('There must be at least {} related values'.format(self.min_related_rev))
            elif len(value) > self.max_related_rev:
                errors.append('There cannot be more than {} related values'.format(self.max_related_rev))
            else:
                for v in value:
                    if not isinstance(v, self.primary_class):
                        errors.append('Related value must be an instance of "{:s}"'.format(self.primary_class.__name__))
                    elif obj not in getattr(v, self.name):
                        errors.append('Object must be in related values')

        if errors:
            return InvalidAttribute(self, errors, related=True)
        return None

    def serialize(self, value):
        """ Serialize related object

        Args:
            value (:obj:`list` of `Model`): Python representation

        Returns:
            :obj:`str`: simple Python representation
        """

        serialized_vals = []
        for v in value:
            primary_attr = v.__class__.Meta.primary_attribute
            serialized_vals.append(primary_attr.serialize(getattr(v, primary_attr.name)))

        serialized_vals.sort(key=natsort_keygen(alg=ns.IGNORECASE))
        return ', '.join(serialized_vals)

    def deserialize(self, values, objects):
        """ Deserialize value

        Args:
            values (:obj:`object`): String representation
            objects (:obj:`dict`): dictionary of objects, grouped by model

        Returns:
            :obj:`tuple` of `object`, `InvalidAttribute` or `None`: tuple of cleaned value and cleaning error
        """
        if not values:
            return (list(), None)

        deserialized_values = list()
        errors = []
        for value in values.split(','):
            value = value.strip()

            related_objs = []
            related_classes = chain([self.related_class], get_subclasses(self.related_class))
            for related_class in related_classes:
                if issubclass(related_class, Model) and value in objects[related_class]:
                    related_objs.append(objects[related_class][value])

            if len(related_objs) == 1:
                deserialized_values.append(related_objs[0])
            elif len(related_objs) == 0:
                primary_attr = self.related_class.Meta.primary_attribute
                errors.append('Unable to find {} with {}={}'.format(
                    self.related_class.__name__, primary_attr.name, quote(value)))
            else:
                errors.append('Multiple matching objects with primary attribute = {}'.format(value))

        if errors:
            return (None, InvalidAttribute(self, errors))
        return (deserialized_values, None)


class RelatedManager(list):
    """ Represent values and related values of related attributes

    Attributes:
        object (:obj:`Model`): model instance
        attribute (:obj:`Attribute`): attribute
        related (:obj:`bool`): is related attribute
    """

    def __init__(self, object, attribute, related=True):
        """
        Args:
            object (:obj:`Model`): model instance
            attribute (:obj:`Attribute`): attribute
            related (:obj:`bool`, optional): is related attribute
        """
        super(RelatedManager, self).__init__()
        self.object = object
        self.attribute = attribute
        self.related = related

    def create(self, **kwargs):
        """ Create instance of primary class and add to list

        Args:
            kwargs (:obj:`dict` of `str`: `object`): dictionary of attribute name/value pairs

        Returns:
            :obj:`Model`: created object

        Raises:
            :obj:`ValueError`: if keyword argument is not an attribute of the class
        """
        if self.related:
            if self.attribute.name in kwargs:
                raise TypeError("'{}' is an invalid keyword argument for {}.create for {}".format(
                    self.attribute.name, self.__class__.__name__, self.attribute.primary_class.__name__))
            obj = self.attribute.primary_class(**kwargs)

        else:
            if self.attribute.related_name in kwargs:
                raise TypeError("'{}' is an invalid keyword argument for {}.create for {}".format(
                    self.attribute.related_name, self.__class__.__name__, self.attribute.primary_class.__name__))
            obj = self.attribute.related_class(**kwargs)

        self.append(obj)

        return obj

    def append(self, value, **kwargs):
        """ Add value to list

        Args:
            value (:obj:`object`): value

        Returns:
            :obj:`RelatedManager`: self
        """
        super(RelatedManager, self).append(value, **kwargs)

        return self

    def add(self, value, **kwargs):
        """ Add value to list

        Args:
            value (:obj:`object`): value

        Returns:
            :obj:`RelatedManager`: self
        """
        self.append(value, **kwargs)

        return self

    def discard(self, value):
        """ Remove value from list if value in list

        Args:
            value (:obj:`object`): value

        Returns:
            :obj:`RelatedManager`: self
        """
        if value in self:
            self.remove(value)

        return self

    def clear(self):
        """ Remove all elements from list

        Returns:
            :obj:`RelatedManager`: self
        """
        for value in reversed(self):
            self.remove(value)

        return self

    def pop(self, i=-1):
        """ Remove an arbitrary element from the list

        Args:
            i (:obj:`int`, optional): index of element to remove

        Returns:
            :obj:`object`: removed element
        """
        value = super(RelatedManager, self).pop(i)
        self.remove(value, update_list=False)

        return value

    def update(self, values):
        """ Add values to list

        Args:
            values (:obj:`list`): values to add to list

        Returns:
            :obj:`RelatedManager`: self
        """
        self.extend(values)

        return self

    def extend(self, values):
        """ Add values to list

        Args:
            values (:obj:`list`): values to add to list

        Returns:
            :obj:`RelatedManager`: self
        """
        for value in values:
            self.append(value)

        return self

    def intersection_update(self, values):
        """ Retain only intersection of list and `values`

        Args:
            values (:obj:`list`): values to intersect with list

        Returns:
            :obj:`RelatedManager`: self
        """
        for value in reversed(self):
            if value not in values:
                self.remove(value)

        return self

    def difference_update(self, values):
        """ Retain only values of list not in `values`

        Args:
            values (:obj:`list`): values to difference with list

        Returns:
            :obj:`RelatedManager`: self
        """
        for value in values:
            if value in self:
                self.remove(value)

        return self

    def symmetric_difference_update(self, values):
        """ Retain values in only one of list and `values`

        Args:
            values (:obj:`list`): values to difference with list

        Returns:
            :obj:`RelatedManager`: self
        """
        self_copy = copy.copy(self)
        values_copy = copy.copy(values)

        for value in values_copy:
            if value in self_copy:
                self.remove(value)
            else:
                self.add(value)

        return self

    def get(self, **kwargs):
        """ Get related objects by attribute/value pairs

        Args:
            **kwargs (:obj:`dict` of `str`:`object`): dictionary of attribute name/value pairs to find matching
                objects

        Returns:
            :obj:`Model` or `None`: matching instance of `Model`, or `None` if no matching instance

        Raises:
            :obj:`ValueError`: if multiple matching objects
        """
        matches = self.filter(**kwargs)

        if len(matches) == 0:
            return None

        if len(matches) == 1:
            return matches.pop()

        if len(matches) > 1:
            raise ValueError('Multiple objects match the attribute name/value pair(s)')

    def filter(self, **kwargs):
        """ Get related objects by attribute/value pairs

        Args:
            **kwargs (:obj:`dict` of `str`:`object`): dictionary of attribute name/value pairs to find matching
                objects

        Returns:
            :obj:`list` of `Model`: matching instances of `Model`
        """
        matches = []

        for obj in self:
            is_match = True
            for attr_name, value in kwargs.items():
                if getattr(obj, attr_name) != value:
                    is_match = False
                    break

            if is_match:
                matches.append(obj)

        return matches

    def index(self, *args, **kwargs):
        """ Get related object index by attribute/value pairs

        Args:
            *args (:obj:`list` of :obj:`Model`): object to find
            **kwargs (:obj:`dict` of :obj:`str`, :obj:`object`): dictionary of attribute name/value pairs to find matching objects

        Returns:
            :obj:`int`: index of matching object

        Raises:
            :obj:`ValueError`: if no argument or keyword argument is provided, if argument and keyword arguments are
                both provided, if multiple arguments are provided, if the keyword attribute/value pairs match no object,
                or if the keyword attribute/value pairs match multiple objects
        """
        if args and kwargs:
            raise ValueError('Argument and keyword arguments cannot both be provided')
        if not args and not kwargs:
            raise ValueError('At least one argument must be provided')

        if args:
            if len(args) > 1:
                raise ValueError('At most one argument can be provided')

            return super(RelatedManager, self).index(args[0])

        else:
            match = None

            for i_obj, obj in enumerate(self):
                is_match = True
                for attr_name, value in kwargs.items():
                    if getattr(obj, attr_name) != value:
                        is_match = False
                        break

                if is_match:
                    if match is not None:
                        raise ValueError('Keyword argument attribute/value pairs match multiple objects')
                    else:
                        match = i_obj

            if match is None:
                raise ValueError('No matching object')

            return match


class ManyToOneRelatedManager(RelatedManager):
    """ Represent values of related attributes """

    def __init__(self, object, attribute):
        """
        Args:
            object (:obj:`Model`): model instance
            attribute (:obj:`Attribute`): attribute
        """
        super(ManyToOneRelatedManager, self).__init__(object, attribute, related=True)

    def append(self, value, propagate=True):
        """ Add value to list

        Args:
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if value in self:
            return self

        super(ManyToOneRelatedManager, self).append(value)
        if propagate:
            value.__setattr__(self.attribute.name, self.object, propagate=True)

        return self

    def remove(self, value, update_list=True, propagate=True):
        """ Remove value from list

        Args:
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if update_list:
            super(ManyToOneRelatedManager, self).remove(value)
        if propagate:
            value.__setattr__(self.attribute.name, None, propagate=False)

        return self


class OneToManyRelatedManager(RelatedManager):
    """ Represent values of related attributes """

    def __init__(self, object, attribute):
        """
        Args:
            object (:obj:`Model`): model instance
            attribute (:obj:`Attribute`): attribute
        """
        super(OneToManyRelatedManager, self).__init__(object, attribute, related=False)

    def append(self, value, propagate=True):
        """ Add value to list

        Args:
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if value in self:
            return self

        super(OneToManyRelatedManager, self).append(value)
        if propagate:
            value.__setattr__(self.attribute.related_name, self.object, propagate=True)

        return self

    def remove(self, value, update_list=True, propagate=True):
        """ Remove value from list

        Args:
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if update_list:
            super(OneToManyRelatedManager, self).remove(value)
        if propagate:
            value.__setattr__(self.attribute.related_name, None, propagate=False)

        return self


class ManyToManyRelatedManager(RelatedManager):
    """ Represent values and related values of related attributes """

    def append(self, value, propagate=True):
        """ Add value to list

        Args:
            value (:obj:`object`): value
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if value in self:
            return self

        super(ManyToManyRelatedManager, self).append(value)
        if propagate:
            if self.related:
                getattr(value, self.attribute.name).append(self.object, propagate=False)
            else:
                getattr(value, self.attribute.related_name).append(self.object, propagate=False)

        return self

    def remove(self, value, update_list=True, propagate=True):
        """ Remove value from list

        Args:
            value (:obj:`object`): value
            update_list (:obj:`bool`, optional): update list
            propagate (:obj:`bool`, optional): propagate change to related attribute

        Returns:
            :obj:`RelatedManager`: self
        """
        if update_list:
            super(ManyToManyRelatedManager, self).remove(value)
        if propagate:
            if self.related:
                getattr(value, self.attribute.name).remove(self.object, propagate=False)
            else:
                getattr(value, self.attribute.related_name).remove(self.object, propagate=False)

        return self


class InvalidObjectSet(object):
    """ Represents a list of invalid objects and invalid models

    Attributes:
        objects (:obj:`list` of `InvalidObject`): list of invalid objects
        models (:obj:`list` of `InvalidModel`): list of invalid models
    """

    def __init__(self, invalid_objects, invalid_models):
        """
        Args:
            invalid_objects (:obj:`list` of `InvalidObject`): list of invalid objects
            invalid_models (:obj:`list` of `InvalidModel`): list of invalid models
        """
        all_invalid_models = set()
        models = [invalid_model.model for invalid_model in invalid_models]
        duplicate_invalid_models = set(mdl for mdl in models
                                       if mdl in all_invalid_models or all_invalid_models.add(mdl))
        if duplicate_invalid_models:
            raise ValueError("duplicate invalid models: {}".format(
                [mdl.__class__.__name__ for mdl in duplicate_invalid_models]))
        self.invalid_objects = invalid_objects or []
        self.invalid_models = invalid_models or []

    def get_object_errors_by_model(self):
        """ Get object errors grouped by model

        Returns:
            :obj:`dict` of `Model`: `list` of `InvalidObject`: dictionary of object errors, grouped by model
        """
        object_errors_by_model = defaultdict(list)
        for obj in self.invalid_objects:
            object_errors_by_model[obj.object.__class__].append(obj)

        return object_errors_by_model

    def get_model_errors_by_model(self):
        """ Get model errors grouped by models

        Returns:
            :obj:`dict` of `Model`: `InvalidModel`: dictionary of model errors, grouped by model
        """
        return {invalid_model.model: invalid_model for invalid_model in self.invalid_models}

    def __str__(self):
        """ Get string representation of errors

        Returns:
            :obj:`str`: string representation of errors
        """

        obj_errs = self.get_object_errors_by_model()
        mdl_errs = self.get_model_errors_by_model()

        models = set(obj_errs.keys())
        models.update(set(mdl_errs.keys()))
        models = natsorted(models, attrgetter('__name__'), alg=ns.IGNORECASE)

        error_forest = []
        for model in models:
            error_forest.append('{}:'.format(model.__name__))

            if model in mdl_errs:
                error_forest.append([str(mdl_errs[model])])

            if model in obj_errs:
                errs = natsorted(obj_errs[model], key=lambda x: x.object.get_primary_attribute(), alg=ns.IGNORECASE)
                error_forest.append([str(obj_err) for obj_err in errs])

        return indent_forest(error_forest)


class InvalidModel(object):
    """ Represents an invalid model, such as a model with an attribute that fails to meet specified constraints

    Attributes:
        model (:obj:`class`): `Model` class
        attributes (:obj:`list` of `InvalidAttribute`): list of invalid attributes and their errors
    """

    def __init__(self, model, attributes):
        """
        Args:
            model (:obj:`class`): `Model` class
            attributes (:obj:`list` of `InvalidAttribute`): list of invalid attributes and their errors
        """
        self.model = model
        self.attributes = attributes

    def __str__(self):
        """ Get string representation of errors

        Returns:
            :obj:`str`: string representation of errors
        """
        attrs = natsorted(self.attributes, key=lambda x: x.attribute.name, alg=ns.IGNORECASE)
        return indent_forest(attrs)


class InvalidObject(object):
    """ Represents an invalid object and its errors

    Attributes:
        object (:obj:`object`): invalid object
        attributes (:obj:`list` of `InvalidAttribute`): list of invalid attributes and their errors
    """

    def __init__(self, object, attributes):
        """
        Args:
            object (:obj:`Model`): invalid object
            attributes (:obj:`list` of `InvalidAttribute`): list of invalid attributes and their errors
        """
        self.object = object
        self.attributes = attributes

    def __str__(self):
        """ Get string representation of errors

        Returns:
            :obj:`str`: string representation of errors
        """
        error_forest = []
        for attr in natsorted(self.attributes, key=lambda x: x.attribute.name, alg=ns.IGNORECASE):
            error_forest.append(attr)
        return indent_forest(error_forest)


class InvalidAttribute(object):
    """ Represents an invalid attribute and its errors

    Attributes:
        attribute (:obj:`Attribute`): invalid attribute
        messages (:obj:`list` of `str`): list of error messages
        related (:obj:`bool`): indicates if error is about value or related value
        location (:obj:`str`, optional): a string representation of the attribute's location in an input file
        value (:obj:`str`, optional): invalid input value
    """

    def __init__(self, attribute, messages, related=False, location=None, value=None):
        """
        Args:
            attribute (:obj:`Attribute`): invalid attribute
            message (:obj:`list` of `str`): list of error messages
            related (:obj:`bool`, optional): indicates if error is about value or related value
            location (:obj:`str`, optional): a string representation of the attribute's location in an
                input file
            value (:obj:`str`, optional): invalid input value
        """
        self.attribute = attribute
        self.messages = messages
        self.related = related
        self.location = location
        self.value = value

    def set_location_and_value(self, location, value):
        """ Set the location and value of the attribute

        Args:
            location (:obj:`str`): a string representation of the attribute's location in an input file
            value (:obj:`str`): the invalid input value
        """
        self.location = location
        if value is None:
            self.value = ''
        else:
            self.value = value

    def __str__(self):
        """ Get string representation of errors

        Returns:
            :obj:`str`: string representation of errors
        """
        if self.related:
            name = "'{}':".format(self.attribute.related_name)
        else:
            name = "'{}':".format(self.attribute.name)

        if self.value is not None:
            name += "'{}'".format(self.value)

        forest = [name]
        if self.location:
            forest.append([self.location,
                           [msg.rstrip() for msg in self.messages]])

        else:
            forest.append([msg.rstrip() for msg in self.messages])

        return indent_forest(forest)


def get_models(module=None, inline=True):
    """ Get models

    Args:
        module (:obj:`module`, optional): module
        inline (:obj:`bool`, optional): if true, return inline models

    Returns:
        :obj:`list` of `class`: list of model classes
    """
    if module:
        models = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Model) and attr is not Model:
                models.append(attr)

    else:
        models = get_subclasses(Model)

    if not inline:
        for model in list(models):
            if model.Meta.tabular_orientation == TabularOrientation.inline:
                models.remove(model)

    return models


def get_model(name, module=None):
    """ Get model with name `name`

    Args:
        name (:obj:`str`): name
        module (:obj:`module`, optional): module

    Returns:
        :obj:`class`: model class
    """
    for model in get_subclasses(Model):
        if name == model.__module__ + '.' + model.__name__ or \
                module is not None and module.__name__ == model.__module__ and name == model.__name__:
            return model

    return None


class Validator(object):
    """ Engine to validate sets of objects """

    def run(self, objects, get_related=False):
        """ Validate a list of objects and return their errors

        Args:
            objects (:obj:`Model` or `list` of `Model`): object or list of objects
            get_related (:obj:`bool`): if true, get all related objects

        Returns:
            :obj:`InvalidObjectSet` or `None`: list of invalid objects/models and their errors
        """
        if isinstance(objects, Model):
            objects = [objects]

        if get_related:
            all_objects = []
            for obj in objects:
                all_objects.extend(obj.get_related())
            objects = list(set(all_objects))

        error = self.clean(objects)
        if error:
            return error
        return self.validate(objects)

    def clean(self, objects):
        """ Clean a list of objects and return their errors

        Args:
            object (:obj:`list` of `Model`): list of objects

        Returns:
            :obj:`InvalidObjectSet` or `None`: list of invalid objects/models and their errors
        """

        object_errors = []
        for obj in objects:
            error = obj.clean()
            if error:
                object_errors.append(error)

        if object_errors:
            return InvalidObjectSet(object_errors, None)

        return None

    def validate(self, objects):
        """ Validate a list of objects and return their errors

        Args:
            object (:obj:`list` of `Model`): list of Model instances

        Returns:
            :obj:`InvalidObjectSet` or `None`: list of invalid objects/models and their errors
        """

        # validate individual objects
        object_errors = []
        for obj in objects:
            error = obj.validate()
            if error:
                object_errors.append(error)

        # group objects by class
        objects_by_class = {}
        for obj in objects:
            for cls in obj.__class__.Meta.inheritance:
                if cls not in objects_by_class:
                    objects_by_class[cls] = []
                objects_by_class[cls].append(obj)

        # validate collections of objects of each Model type
        model_errors = []
        for cls, cls_objects in objects_by_class.items():
            error = cls.validate_unique(cls_objects)
            if error:
                model_errors.append(error)

        # return errors
        if object_errors or model_errors:
            return InvalidObjectSet(object_errors, model_errors)

        return None


def excel_col_name(col):
    """ Convert column number to an Excel-style string.

    From http://stackoverflow.com/a/19169180/509882
    """
    LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    if not isinstance(col, int) or col < 1:
        raise ValueError("excel_col_name: col ({}) must be a positive integer".format(col))

    result = []
    while col:
        col, rem = divmod(col - 1, 26)
        result[:0] = LETTERS[rem]
    return ''.join(result)


class InvalidWorksheet(object):
    """ Represents an invalid worksheet or delimiter-separated file and its errors

    Attributes:
        filename (:obj:`str`): filename of file containing the invalid worksheet
        name (:obj:`str`): name of the invalid worksheet
        errors (:obj:`list` of `str`): list of the worksheet's errors
    """

    def __init__(self, filename, name, errors):
        """
        Args:
            filename (:obj:`str`): filename of file containing the invalid worksheet
            name (:obj:`str`): name of the invalid worksheet
            errors (:obj:`list` of `str`): list of the worksheet's errors
        """
        self.filename = filename
        self.name = name
        self.errors = errors

    def __str__(self):
        """ Get string representation of an `InvalidWorksheet`

        Returns:
            :obj:`str`: string representation of an `InvalidWorksheet`
        """
        error_forest = ["'{}':'{}':".format(self.filename, self.name)]
        error_forest.append(self.errors)
        return indent_forest(error_forest)


class SchemaWarning(UserWarning):
    """ Schema warning """
    pass
