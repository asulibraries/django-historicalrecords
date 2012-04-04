from django.db import models


class HistoryDescriptor(object):
    def __init__(self, model):
        self.model = model

    def __get__(self, instance, owner):
        return HistoryManager(self.model, owner, instance)


class HistoryManager(models.Manager):
    def __init__(self, model, primary_model, instance=None):
        super(HistoryManager, self).__init__()
        self.model = model
        self.primary_model = primary_model
        self.instance = instance

    def get_query_set(self):
        qs = super(HistoryManager, self).get_query_set()
        if self.instance:
            qs = self._filter_queryset_by_pk(qs, self.instance.pk)
        return qs

    def _filter_queryset_by_pk(self, qs, pk):
        return qs.filter(**{self.primary_model._meta.pk.name: pk})

    def most_recent(self, pk=None):
        """
        If called with an instance, returns the most recent copy of the instance
        available in the history.

          >>> obj = Obj.objects.get(pk=1)
          >>> obj.history.most_recent()
          <Obj...>

        If called without an instance, returns the most recent copy of an
        instance matching pk.

          >>> Obj.history.most_recent(pk=1)
          <Obj...>
        """
        pk = self.instance.pk if self.instance else pk
        qs = self._filter_queryset_by_pk(self.get_query_set(), pk)

        try:
            version = qs[0]
        except IndexError:
            message = "%s(pk=%s) has no historical record." % \
                (self.primary_model.__name__, pk)
            raise self.primary_model.DoesNotExist(message)
        else:
            return version.history_object

    def as_of(self, date, pk=None, restore=False):
        """
        If called with an instance, returns an instance of the original model
        with all the attributes set to what was present on the object on the
        date provided.

          >>> obj = Obj.objects.get(pk=1)
          >>> obj.history.as_of(datetime.datetime(2000, 1, 1))
          <Obj...>

        If called without an instance, has similar behavior but does its lookup
        based on the pk provided.

          >>> Obj.history.as_of(datetime.datetime(2000, 1, 1), pk=1)
          <Obj...>
        """
        pk = self.instance.pk if self.instance else pk
        qs = self._filter_queryset_by_pk(self.get_query_set(), pk)

        try:
            version = qs.filter(history_date__lte=date)[0]
        except IndexError:
            message = "%s(pk=%s) had not yet been created." % \
                (self.primary_model.__name__, pk)
            raise self.primary_model.DoesNotExist(message)
        else:
            from history.models import DELETED
            if version.history_type == DELETED and not restore:
                message = "%s(pk=%s) had already been deleted." % \
                    (self.primary_model.__name__, pk)
                raise self.primary_model.DoesNotExist(message)
            return version.history_object

    @property
    def created_date(self):
        if not self.instance:
            raise TypeError("Can't use created_date() without a %s instance." % \
                                self.primary_model._meta.object_name)
        return self.aggregate(created=models.Min('history_date'))['created']

    @property
    def created_by(self):
        if not self.instance:
            raise TypeError("Can't use created_by() without a %s instance." % \
                                self.primary_model._meta.object_name)
        return self.order_by('history_date')[0].history_editor

    @property
    def last_modified_date(self):
        if not self.instance:
            raise TypeError("Can't use last_modified_date() without a %s instance." % \
                                self.primary_model._meta.object_name)

        return self.aggregate(modified=models.Max('history_date'))['modified']

    @property
    def last_modified_by(self):
        if not self.instance:
            raise TypeError("Can't use last_modified_by() without a %s instance." % \
                                self.primary_model._meta.object_name)
        return self.order_by('-history_date')[0].history_editor

    def get_or_restore(self, pk):
        '''
        Looks for an existing item with the given primary key in the primary
        object table - return it if it exists, otherwise try to 'restore' the
        most recent version of the item.
        '''
        if self.instance:
            raise TypeError("Can't use get_or_restore() with a %s instance." %\
                            self.instance._meta.object_name)
        try:
            return self.primary_model._default_manager.get(pk=pk)
        except self.primary_model.DoesNotExist:
            return self.most_recent(pk=pk)


class HistoricalAnnotatingManager(models.Manager):

    def get_query_set(self):
        '''
        Annotate the queryset with historical information:
         - created_date - the history_date of the earliest version
         - last_modified_date - the history_date of the most recent version
         - count - the number of historical versions
        '''
        return super(HistoricalAnnotatingManager, self)\
            .get_query_set()\
            .annotate(created_date=models.Min('history__history_date'))\
            .annotate(last_modified_date=models.Max('history__history_date'))\
            .annotate(count=models.Count('history'))
