import copy
from functools import wraps

from django.contrib.auth.models import User
import django.db
from django.db import models
from django.db.models.base import ModelBase
from django.db.models.fields.related import add_lazy_relation
from django.db.models.loading import app_cache_ready, AppCache
from django.db.models.related import RelatedObject

from history import manager

# Behaviors for foreign key conversion.
PRESERVE = 1
CONVERT = 2

CREATED = '+'
MODIFIED = '~'
DELETED = '-'
HISTORY_TYPES = (
    (CREATED, 'Created'),
    (MODIFIED, 'Modified'),
    (DELETED, 'Deleted')
)


class HistoryChange(object):
    def __init__(self, name, from_value, to_value, verbose_name):
        self.name = name
        self.from_value = from_value
        self.to_value = to_value
        self.verbose_name = verbose_name

    def __unicode__(self):
        return 'Field "%s" changed from "%s" to "%s"' % \
            (self.name, self.from_value, self.to_value)


class HistoricalRecords(object):
    """
    Usage:
    class MyModel(models.Model):
        ...
        history = HistoricalRecords()

    Parameters:
    - (optional) module: act like this model was defined in another module.
                         (This will be reported to Django and South for
                         migrations, and table names.)
    - (optional) fields: a list of field names to be checked and saved. If
                         nothing is defined, all fields will be saved.
    """

    # meta -> (model, manager_name, history_model)
    REGISTRY = {}

    def __init__(self,
                 module=None,
                 fields=None,
                 key_conversions=None,
                 add_history_properties=False,
                 require_editor=False):
        self._module = module
        self._fields = fields
        self.key_conversions = key_conversions or {}
        self.add_history_properties = add_history_properties
        self.require_editor = require_editor

    def contribute_to_class(self, cls, name):
        self.manager_name = name
        models.signals.class_prepared.connect(self.model_prepared, sender=cls)

    def model_prepared(self, sender, **kwargs):
        '''
        Wait for any field dependencies to be resolved, then call finalize().
        '''
        model = sender
        deps = self.get_field_dependencies(model)
        if deps:
            count = [len(deps)]

            def dependency_resolved(*args):
                count[0] = count[0] - 1
                if count[0] == 0:
                    self.finalize(model)

            for dep in deps:
                add_lazy_relation(model, None, dep.rel.to, dependency_resolved)
        else:
            self.finalize(model)

    def finalize(self, model):
        # The HistoricalRecords object will be discarded,
        # so the signal handlers can't use weak references.
        models.signals.post_save.connect(self.post_save, sender=model,
                                         weak=False)
        models.signals.post_delete.connect(self.post_delete, sender=model,
                                           weak=False)

        history_model = self.create_history_model(model)
        descriptor = manager.HistoryDescriptor(history_model)
        setattr(model, self.manager_name, descriptor)
        self.monkey_patch_name_map(model)

        if self.add_history_properties:
            self.monkey_patch_history_properties(model)

        self.capture_save_method(model)
        self.capture_delete_method(model)
        self.capture_init(model)
        self.create_set_editor_method(model)

        if model._meta in HistoricalRecords.REGISTRY:
            AppCache().app_errors[model._meta] = 'Models cannot have more than one HistoricalRecords field.'
        else:
            regvalue = model, self.manager_name, history_model
            HistoricalRecords.REGISTRY[model._meta] = regvalue

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

        created_by = lambda m: getattr(m, self.manager_name).created_by
        cls.created_by = property(created_by)

        last_modified_by = lambda m: getattr(m, self.manager_name).last_modified_by
        cls.last_modified_by = property(last_modified_by)

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
        opts = cls._meta.__class__
        original_init_name_map = opts.init_name_map

        @wraps(original_init_name_map)
        def new_init_name_map(meta):
            original_map = original_init_name_map(meta)
            updated_map = self.update_item_name_map(original_map, meta)
            if original_map != updated_map and app_cache_ready():
                meta._name_map = updated_map
                return updated_map
            return original_map

        # this may be called multiple times - only patch once
        if opts.init_name_map.func_code != new_init_name_map.func_code:
            opts.init_name_map = new_init_name_map

    def update_item_name_map(self, map, meta):
        if meta not in HistoricalRecords.REGISTRY:
            return map

        # item is registered as a history item, see if it needs to
        # update the item name map
        model, mgr, hmodel = HistoricalRecords.REGISTRY.get(meta)

        # inject additional lookup into item name map
        history_fk = models.ForeignKey(model)
        history_fk.column = meta.pk.get_attname()
        history_fk.model = hmodel
        rel = RelatedObject(model, hmodel, history_fk)

        m = dict(map)
        m[mgr] = (rel, None, False, False)
        return m

    def capture_save_method(self, model):
        """
        Replace 'save()' by 'save(editor=user)'
        """
        original_save = model.save
        require_editor = self.require_editor

        @wraps(original_save)
        def new_save(self, *args, **kwargs):
            # Save editor in temporary variable, post_save will read this one
            self._history_editor = kwargs.pop('editor', getattr(self, '_history_editor', None))
            if require_editor and not self._history_editor:
                raise ValueError('Editor field is required')
            original_save(self, *args, **kwargs)

        model.save = new_save

    def capture_delete_method(self, model):
        """
        Replace 'delete()' by 'delete(editor=user)'
        """
        original_delete = model.delete
        require_editor = self.require_editor

        @wraps(original_delete)
        def new_delete(self, *args, **kwargs):
            # Save editor in temporary variable, post_delete will read this one
            self._history_editor = kwargs.pop('editor', getattr(self, '_history_editor', None))
            if require_editor and not self._history_editor:
                raise ValueError('Editor field is required')
            original_delete(self, *args, **kwargs)

        model.delete = new_delete

    def capture_init(self, model):
        """
        Allow editor kwarg in create()
        """
        original_init = model.__init__

        @wraps(original_init)
        def new_init(self, *args, **kwargs):
            # Save editor in temporary variable, post_save will read this one
            self._history_editor = kwargs.pop('editor', None)
            original_init(self, *args, **kwargs)

        model.__init__ = new_init

    def create_set_editor_method(self, model):
        """
        Add a set_editor method to the model which has a history.
        """
        if hasattr(model, 'set_editor'):
            raise Exception('historicalrecords cannot add method set_editor to %s' % model.__class__.__name__)

        def set_editor(self, editor):
            """
            Set the editor (User object) to be used in the historicalrecord during the next save() call.
            """
            self._history_editor = editor
        model.set_editor = set_editor

    def create_history_model(self, model):
        """
        Creates a historical model to associate with the model provided.
        """
        # rel_nm = '_%s_history' % model._meta.object_name.lower()
        rel_nm_user = '_%s_history_editor' % model._meta.object_name.lower()
        important_field_names = self.get_important_field_names(model)

        def get_verbose_name(model, field_name):
            for f in model._meta.fields:
                if f.name == field_name:
                    return f.verbose_name

        class HistoryEntryMeta(ModelBase):
            """
            Meta class for history model. This will rename the history model,
            and copy the necessary fields from the other model.
            """
            def __new__(c, name, bases, attrs):
                # Rename class
                name = 'Historical%s' % model._meta.object_name

                # This attribute is required for a model to function properly.
                attrs['__module__'] = self._module or model.__module__

                # Copy attributes from base class
                attrs.update(self.copy_fields(model))

                return ModelBase.__new__(c, name, bases, attrs)

        class HistoryEntry(models.Model):
            """
            History entry
            """
            __metaclass__ = HistoryEntryMeta

            class Meta:
                ordering = ['-history_id']
                get_latest_by = 'history_id'

            history_id = models.AutoField(primary_key=True)
            history_date = models.DateTimeField(auto_now_add=True,
                                                db_index=True)
            history_type = models.CharField(max_length=1, choices=HISTORY_TYPES)
            history_editor = models.ForeignKey(User, null=True, blank=True,
                                               related_name=rel_nm_user)
            primary_model = model

            def __unicode__(self):
                return u'%s as of %s' % (self.history_object, self.history_date)

            @property
            def previous_entry(self):
                try:
                    return self.history_object.history.order_by('-history_id').filter(history_id__lt=self.history_id)[0]
                except IndexError:
                    return None

            @property
            def modified_fields(self):
                """
                Return a list of which field have been changed during this save.
                """
                previous_entry = self.previous_entry
                if previous_entry:
                    modified = []
                    for field in important_field_names:
                        from_value = getattr(previous_entry, field)
                        to_value = getattr(self, field)
                        if from_value != to_value:
                            modified.append(HistoryChange(field, from_value, to_value, get_verbose_name(model, field)))
                    return modified
                else:
                    # No previous history entry, so actually everything has been modified.
                    return [HistoryChange(f, None, getattr(self, f), get_verbose_name(model, f)) for f in important_field_names]

        # create the descriptor for 'history_object' with the new HistoryEntry
        HistoryEntry.history_object = HistoricalObjectDescriptor(HistoryEntry)
        HistoryEntry.important_field_names = important_field_names

        return HistoryEntry

    def get_field_dependencies(self, model):
        deps = []
        for field in model._meta.fields:
            if isinstance(field, models.ForeignKey):
                deps.append(field)
        return deps

    def get_important_fields(self, model):
        """ Return the list of fields that we care about.  """
        for f in model._meta.fields:
            #import pdb; pdb.set_trace();
            if f == model._meta.pk or not self._fields or f.name in self._fields:
                yield f

    def get_important_field_names(self, model):
        """ Return the names of the fields that we care about.  """
        return [f.attname for f in self.get_important_fields(model)]

    def copy_fields(self, model):
        """
        Creates copies of the model's original fields, returning
        a dictionary mapping field name to copied field object.
        """
        fields = {}
        for field in self.get_important_fields(model):
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

            # TODO: one-to-one field

            fields[field.name] = field

        return fields

    def post_save(self, instance, created, **kwargs):
        """
        During post-save, create historical record if none has been created before,
        or when the saved instance has fields which differ from the most recent
        historicalrecord.
        """
        # Decide whether to save a history copy: only when certain fields were changed.
        save = True
        try:
            most_recent = getattr(instance, self.manager_name).most_recent()
            save = False
            for field in self.get_important_field_names(instance):
                if getattr(instance, field) != getattr(most_recent, field):
                    save = True
        except instance.DoesNotExist:
            pass

        # Create historical record
        if save:
            self.create_historical_record(instance, instance._history_editor, created and CREATED or MODIFIED)

    def post_delete(self, instance, **kwargs):
        try:
            self.create_historical_record(instance, instance._history_editor, DELETED)
        except HistoricalIntegrityError:
            pass

    def create_historical_record(self, instance, editor, type):
        manager = getattr(instance, self.manager_name)
        attrs = {}
        for field in self.get_important_fields(instance):
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
        manager.create(history_type=type, history_editor=editor, **attrs)


class HistoricalObjectDescriptor(object):
    def __init__(self, history_model):
        self.history_model = history_model

    def __get__(self, instance, owner):
        values = dict((f, getattr(instance, f)) for f in
                       self.history_model.important_field_names)
        return self.history_model.primary_model(**values)


class HistoricalIntegrityError(django.db.IntegrityError):
    pass
