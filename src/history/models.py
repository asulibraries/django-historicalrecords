import copy
import datetime

import django.db
from django.db import models
from django.db.models.fields.related import add_lazy_relation, RelatedField
from django.db.models.loading import app_cache_ready, AppCache
from django.db.models.related import RelatedObject

from history import manager

# Behaviors for foreign key conversion.
PRESERVE = 1
CONVERT = 2

class HistoricalRecords(object):

    PATCHED_META_CLASSES = {}
    HISTORICAL_RECORD_CLASSES = {}

    def __init__(self, key_conversions=None,
                 add_history_properties=False):
        self.key_conversions = key_conversions or {}
        self.add_history_properties = add_history_properties

    def contribute_to_class(self, cls, name):
        if cls._meta in self.HISTORICAL_RECORD_CLASSES:
            AppCache().app_errors[cls._meta] = 'Models cannot have more than one HistoricalRecords field.'
        self.HISTORICAL_RECORD_CLASSES[cls._meta] = True
        
        self.manager_name = name
        models.signals.class_prepared.connect(self.finalize, sender=cls)

    def monkey_patch_history_properties(self, cls):
        '''
        Add 'created_date' and 'last_modified_date' properties to the model
        we're managing history for, calling the underlying manager to get the 
        values.
        '''
        created_date = lambda m: getattr(m, self.manager_name).created_date
        cls.created_date = property(created_date)

        last_modified_date = lambda m: getattr(m, self.manager_name).last_modified_date
        cls.last_modified_date = property(last_modified_date)

    def monkey_patch_name_map(self, cls):
        '''
        Replace init_name_map() with a custom implementation, allowing us to
        trick Django into recognizing a phantom history relation that can
        be used in chained filters, annotations, etc.

        Examples:

        # Annotate the Foo results with a 'history_length' containing the
        # number of versions in each object's history
        >>> Foo.objects.annotate(history_length=Count('history'))

        # Get a list of Bar objects whose 'value' property has been over 9000
        # at some point in time.
        >>> Bar.objects.filter(history__value__gt=9000)
        '''
        if cls._meta.__class__ in self.PATCHED_META_CLASSES:
            return

        original_init_name_map = cls._meta.__class__.init_name_map
        def init_name_map(meta):
            original_map = original_init_name_map(meta)
            updated_map = self.update_item_name_map(original_map, cls)

            if original_map != updated_map and app_cache_ready():
                meta._name_map = updated_map
                return updated_map
            
            return original_map
        cls._meta.__class__.init_name_map = init_name_map
        
        # keep track of the fact that we patched this so we don't patch
        # it multiple times
        self.PATCHED_META_CLASSES[cls._meta.__class__] = True

    def update_item_name_map(self, map, cls):

        # inject additional lookup into item name map
        history_model = getattr(cls, self.manager_name).model
        history_fk = models.ForeignKey(cls)
        history_fk.column = cls._meta.pk.get_attname()
        history_fk.model = history_model
        rel = RelatedObject(cls, history_model, history_fk)

        m = dict(map)
        m[self.manager_name] = (rel, None, False, False)
        return m

    def get_field_dependencies(self, model):
        deps = []
        for field in model._meta.fields: 
            if isinstance(field, models.ForeignKey):
                deps.append(field)
        return deps

    def finalize(self, sender, **kwargs):
        
        # The HistoricalRecords object will be discarded,
        # so the signal handlers can't use weak references.
        models.signals.post_save.connect(self.post_save, sender=sender,
                                         weak=False)
        models.signals.post_delete.connect(self.post_delete, sender=sender,
                                           weak=False)

        def _finalize():
            history_model = self.create_history_model(sender)
            descriptor = manager.HistoryDescriptor(history_model)
            setattr(sender, self.manager_name, descriptor)
            self.monkey_patch_name_map(sender)

            if self.add_history_properties:
                self.monkey_patch_history_properties(sender)

        deps = self.get_field_dependencies(sender)
        if deps:
            count = [len(deps)] 
            def dependency_resolved(*args):
                count[0] = count[0] - 1
                if count[0] == 0:
                    _finalize()

            for dep in deps:
                add_lazy_relation(sender, None, dep.rel.to, dependency_resolved)
        else:
            _finalize()

    def create_history_model(self, model):
        """
        Creates a historical model to associate with the model provided.
        """
        attrs = self.copy_fields(model)
        attrs.update(self.get_extra_fields(model))
        attrs.update(Meta=type('Meta', (), self.get_meta_options(model)))
        name = 'Historical%s' % model._meta.object_name
        return type(name, (models.Model,), attrs)

    def copy_fields(self, model):
        """
        Creates copies of the model's original fields, returning
        a dictionary mapping field name to copied field object.
        """
        # Though not strictly a field, this attribute
        # is required for a model to function properly.
        fields = {'__module__': model.__module__}

        for field in model._meta.fields:
            field = copy.copy(field)
            
            # Deal with foreign keys, optionally according to a configured
            # behavior scheme.
            if isinstance(field, models.ForeignKey):
                conversion = self.key_conversions.get(field.name, CONVERT)
                if conversion == CONVERT:
                    # Convert the ForeignKey to a plain primary key field
                    options = {
                      'null': field.null,
                      'blank': field.blank,
                      'name': field.get_attname(),
                    }
                    field = copy.copy(field.rel.to._meta.pk)
                    [setattr(field, key, options[key]) for key in options]

                elif conversion == PRESERVE:
                    # Preserve ForeignKey relationships with a reasonable 
                    # related_name, fixing a syncdb issue.
                    rel = copy.copy(field.rel)
                    related_name = rel.related_name or field.opts.object_name.lower()
                    rel.related_name = related_name + '_historical'
                    field.rel = rel
                else:
                    # This should never happen, let's make sure!
                    raise ValueError('Invalid key conversion type')
            
            if isinstance(field, models.AutoField):
                # The historical model gets its own AutoField, so any
                # existing one must be replaced with an IntegerField.
                field.__class__ = models.IntegerField

            if isinstance(field, models.DateField) or \
                    isinstance(field, models.TimeField):
                field.auto_now = False
                field.auto_now_add = False

            if field.primary_key or field.unique:
                # Unique fields can no longer be guaranteed unique,
                # but they should still be indexed for faster lookups.
                field.primary_key = False
                field._unique = False
                field.db_index = True
            fields[field.name] = field

        return fields

    def get_extra_fields(self, model):
        """
        Returns a dictionary of fields that will be added to the historical
        record model, in addition to the ones returned by copy_fields below.
        """
        rel_nm = '_%s_history' % model._meta.object_name.lower()
        return {
            'history_id': models.AutoField(primary_key=True),
            'history_date': models.DateTimeField(default=datetime.datetime.now,
                                                 db_index=True),
            'history_type': models.CharField(max_length=1, choices=(
                ('+', 'Created'),
                ('~', 'Changed'),
                ('-', 'Deleted'),
            )),
            'history_object': HistoricalObjectDescriptor(model),
            'primary_model': model,
            '__unicode__': lambda self: u'%s as of %s' % (self.history_object,
                                                          self.history_date)
        }

    def get_meta_options(self, model):
        """
        Returns a dictionary of fields that will be added to
        the Meta inner class of the historical record model.
        """
        return {
            'ordering': ('-history_id',),
            'get_latest_by': 'history_id'
        }

    def post_save(self, instance, created, **kwargs):
        self.create_historical_record(instance, created and '+' or '~')

    def post_delete(self, instance, **kwargs):
        try:
            self.create_historical_record(instance, '-')
        except HistoricalIntegrityError:
            pass

    def create_historical_record(self, instance, type):
        manager = getattr(instance, self.manager_name)
        attrs = {}
        for field in instance._meta.fields:
            '''
            Detect a condition where a cascading delete causes an integrity
            error because the post_delete trigger tries to create a 
            reference to a now-deleted instance in its history record.  This
            should only be an issue on PRESERVEd foreign keys, since CONVERTed
            ones won't have an explicit reference.  
            
            Raise a specific exception when the condition is detected, allowing
            post_delete to ignore historical record creation in this case.
            '''
            if isinstance(field, models.ForeignKey):
                conversion = self.key_conversions.get(field.name, CONVERT)
                if conversion == PRESERVE:
                    try:
                        # dereference key to make sure it exists
                        getattr(instance, field.name) 
                    except field.rel.to.DoesNotExist as e:
                        raise HistoricalIntegrityError(e)

            # copy field values normally
            attrs[field.attname] = getattr(instance, field.attname)
        manager.create(history_type=type, **attrs)

class HistoricalObjectDescriptor(object):
    def __init__(self, model):
        self.model = model

    def __get__(self, instance, owner):
        values = (getattr(instance, f.attname) for f in self.model._meta.fields)
        return self.model(*values)

class HistoricalIntegrityError(django.db.IntegrityError):
    pass
