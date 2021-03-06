# -*- coding: utf-8 -*-

__author__ = 'damien'

import datetime
import re

import tg
from tg.i18n import ugettext as _

import sqlalchemy
from sqlalchemy.orm import aliased
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import get_history
from sqlalchemy import desc
from sqlalchemy import distinct
from sqlalchemy import not_
from sqlalchemy import or_
from tracim.lib import cmp_to_key
from tracim.lib.notifications import NotifierFactory
from tracim.lib.utils import SameValueError
from tracim.model import DBSession
from tracim.model.auth import User
from tracim.model.data import ActionDescription
from tracim.model.data import BreadcrumbItem
from tracim.model.data import ContentStatus
from tracim.model.data import ContentRevisionRO
from tracim.model.data import Content
from tracim.model.data import ContentType
from tracim.model.data import NodeTreeItem
from tracim.model.data import RevisionReadStatus
from tracim.model.data import UserRoleInWorkspace
from tracim.model.data import Workspace

def compare_content_for_sorting_by_type_and_name(content1: Content,
                                                 content2: Content):
    """
    :param content1:
    :param content2:
    :return:    1 if content1 > content2
                -1 if content1 < content2
                0 if content1 = content2
    """

    if content1.type==content2.type:
        if content1.get_label().lower()>content2.get_label().lower():
            return 1
        elif content1.get_label().lower()<content2.get_label().lower():
            return -1
        return 0
    else:
        # TODO - D.A. - 2014-12-02 - Manage Content Types Dynamically
        content_type_order = [ContentType.Folder, ContentType.Page, ContentType.Thread, ContentType.File]
        result = content_type_order.index(content1.type)-content_type_order.index(content2.type)
        if result<0:
            return -1
        elif result>0:
            return 1
        else:
            return 0

def compare_tree_items_for_sorting_by_type_and_name(item1: NodeTreeItem, item2: NodeTreeItem):
    return compare_content_for_sorting_by_type_and_name(item1.node, item2.node)


class ContentApi(object):

    SEARCH_SEPARATORS = ',| '
    SEARCH_DEFAULT_RESULT_NB = 50


    def __init__(self, current_user: User, show_archived=False, show_deleted=False, all_content_in_treeview=True):
        self._user = current_user
        self._user_id = current_user.user_id if current_user else None
        self._show_archived = show_archived
        self._show_deleted = show_deleted
        self._show_all_type_of_contents_in_treeview = all_content_in_treeview


    @classmethod
    def sort_tree_items(cls, content_list: [NodeTreeItem])-> [Content]:
        news = []
        for item in content_list:
            news.append(item)

        content_list.sort(key=cmp_to_key(compare_tree_items_for_sorting_by_type_and_name))

        return content_list


    @classmethod
    def sort_content(cls, content_list: [Content])-> [Content]:
        content_list.sort(key=cmp_to_key(compare_content_for_sorting_by_type_and_name))
        return content_list

    def build_breadcrumb(self, workspace, item_id=None, skip_root=False) -> [BreadcrumbItem]:
        """
        TODO - Remove this and factorize it with other get_breadcrumb_xxx methods
        :param item_id: an item id (item may be normal content or folder
        :return:
        """
        workspace_id = workspace.workspace_id
        breadcrumb = []

        if not skip_root:
            breadcrumb.append(BreadcrumbItem(ContentType.get_icon(ContentType.FAKE_Dashboard), _('Workspaces'), tg.url('/workspaces')))
        breadcrumb.append(BreadcrumbItem(ContentType.get_icon(ContentType.FAKE_Workspace), workspace.label, tg.url('/workspaces/{}'.format(workspace.workspace_id))))

        if item_id:
            breadcrumb_folder_items = []
            current_item = self.get_one(item_id, ContentType.Any, workspace)
            is_active = True
            if current_item.type==ContentType.Folder:
                next_url = tg.url('/workspaces/{}/folders/{}'.format(workspace_id, current_item.content_id))
            else:
                next_url = tg.url('/workspaces/{}/folders/{}/{}s/{}'.format(workspace_id, current_item.parent_id, current_item.type, current_item.content_id))

            while current_item:
                breadcrumb_item = BreadcrumbItem(ContentType.get_icon(current_item.type),
                                                 current_item.label,
                                                 next_url,
                                                 is_active)
                is_active = False # the first item is True, then all other are False => in the breadcrumb, only the last item is "active"
                breadcrumb_folder_items.append(breadcrumb_item)
                current_item = current_item.parent
                if current_item:
                    # In last iteration, the parent is None, and there is no more breadcrumb item to build
                    next_url = tg.url('/workspaces/{}/folders/{}'.format(workspace_id, current_item.content_id))

            for item in reversed(breadcrumb_folder_items):
                breadcrumb.append(item)


        return breadcrumb

    def __real_base_query(self, workspace: Workspace=None):
        result = DBSession.query(Content)

        if workspace:
            result = result.filter(Content.workspace_id==workspace.workspace_id)

        if self._user:
            user = DBSession.query(User).get(self._user_id)
            # Filter according to user workspaces
            workspace_ids = [r.workspace_id for r in user.roles \
                             if r.role>=UserRoleInWorkspace.READER]
            result = result.filter(Content.workspace_id.in_(workspace_ids))

        return result

    def _base_query(self, workspace: Workspace=None):
        result = self.__real_base_query(workspace)

        if not self._show_deleted:
            result = result.filter(Content.is_deleted==False)

        if not self._show_archived:
            result = result.filter(Content.is_archived==False)

        return result

    def __revisions_real_base_query(self, workspace: Workspace=None):
        result = DBSession.query(ContentRevisionRO)

        if workspace:
            result = result.filter(ContentRevisionRO.workspace_id==workspace.workspace_id)

        if self._user:
            user = DBSession.query(User).get(self._user_id)
            # Filter according to user workspaces
            workspace_ids = [r.workspace_id for r in user.roles \
                             if r.role>=UserRoleInWorkspace.READER]
            result = result.filter(ContentRevisionRO.workspace_id.in_(workspace_ids))

        return result

    def _revisions_base_query(self, workspace: Workspace=None):
        result = self.__revisions_real_base_query(workspace)

        if not self._show_deleted:
            result = result.filter(ContentRevisionRO.is_deleted==False)

        if not self._show_archived:
            result = result.filter(ContentRevisionRO.is_archived==False)

        return result

    def _hard_filtered_base_query(self, workspace: Workspace=None):
        """
        If set to True, then filterign on is_deleted and is_archived will also
        filter parent properties. This is required for search() function which
        also search in comments (for example) which may be 'not deleted' while
        the associated content is deleted

        :param hard_filtering:
        :return:
        """
        result = self.__real_base_query(workspace)

        if not self._show_deleted:
            parent = aliased(Content)
            result = result.join(parent, Content.parent).\
                filter(Content.is_deleted==False).\
                filter(parent.is_deleted==False)

        if not self._show_archived:
            parent = aliased(Content)
            result = result.join(parent, Content.parent).\
                filter(Content.is_archived==False).\
                filter(parent.is_archived==False)

        return result

    def get_child_folders(self, parent: Content=None, workspace: Workspace=None, filter_by_allowed_content_types: list=[], removed_item_ids: list=[], allowed_node_types=None) -> [Content]:
        """
        This method returns child items (folders or items) for left bar treeview.

        :param parent:
        :param workspace:
        :param filter_by_allowed_content_types:
        :param removed_item_ids:
        :param allowed_node_types: This parameter allow to hide folders for which the given type of content is not allowed.
               For example, if you want to move a Page from a folder to another, you should show only folders that accept pages
        :return:
        """
        if not allowed_node_types:
            allowed_node_types = [ContentType.Folder]
        elif allowed_node_types==ContentType.Any:
            allowed_node_types = ContentType.all()

        parent_id = parent.content_id if parent else None
        folders = self._base_query(workspace).\
            filter(Content.parent_id==parent_id).\
            filter(Content.type.in_(allowed_node_types)).\
            filter(Content.content_id.notin_(removed_item_ids)).\
            all()

        if not filter_by_allowed_content_types or \
                        len(filter_by_allowed_content_types)<=0:
            # Standard case for the left treeview: we want to show all contents
            # in the left treeview... so we still filter because for example
            # comments must not appear in the treeview
            return [folder for folder in folders \
                    if folder.type in ContentType.allowed_types_for_folding()]

        # Now this is a case of Folders only (used for moving content)
        # When moving a content, you must get only folders that allow to be filled
        # with the type of content you want to move
        result = []
        for folder in folders:
            for allowed_content_type in filter_by_allowed_content_types:
                if folder.type==ContentType.Folder and folder.properties['allowed_content'][allowed_content_type]==True:
                    result.append(folder)
                    break

        return result

    def create(self, content_type: str, workspace: Workspace, parent: Content=None, label:str ='', do_save=False) -> Content:
        assert content_type in ContentType.allowed_types()
        content = Content()
        content.owner = self._user
        content.parent = parent
        content.workspace = workspace
        content.type = content_type
        content.label = label
        content.revision_type = ActionDescription.CREATION

        if do_save:
            DBSession.add(content)
            self.save(content, ActionDescription.CREATION)
        return content


    def create_comment(self, workspace: Workspace=None, parent: Content=None, content:str ='', do_save=False) -> Content:
        assert parent  and parent.type!=ContentType.Folder
        item = Content()
        item.owner = self._user
        item.parent = parent
        item.workspace = workspace
        item.type = ContentType.Comment
        item.description = content
        item.label = ''
        item.revision_type = ActionDescription.COMMENT

        if do_save:
            self.save(item, ActionDescription.COMMENT)
        return item


    def get_one_from_revision(self, content_id: int, content_type: str, workspace: Workspace=None, revision_id=None) -> Content:
        """
        This method is a hack to convert a node revision item into a node
        :param content_id:
        :param content_type:
        :param workspace:
        :param revision_id:
        :return:
        """

        content = self.get_one(content_id, content_type, workspace)
        revision = DBSession.query(ContentRevisionRO).filter(ContentRevisionRO.revision_id==revision_id).one()

        if revision.content_id==content.content_id:
            content.revision_to_serialize = revision.revision_id
        else:
            raise ValueError('Revision not found for given content')

        return content

    def get_one(self, content_id: int, content_type: str, workspace: Workspace=None) -> Content:

        if not content_id:
            return None

        if content_type==ContentType.Any:
            return self._base_query(workspace).filter(Content.content_id==content_id).one()

        return self._base_query(workspace).filter(Content.content_id==content_id).filter(Content.type==content_type).one()

    def get_all(self, parent_id: int, content_type: str, workspace: Workspace=None) -> Content:
        assert parent_id is None or isinstance(parent_id, int) # DYN_REMOVE
        assert content_type is not None# DYN_REMOVE
        assert isinstance(content_type, str) # DYN_REMOVE

        resultset = self._base_query(workspace)

        if content_type!=ContentType.Any:
            resultset = resultset.filter(Content.type==content_type)

        if parent_id:
            resultset = resultset.filter(Content.parent_id==parent_id)

        return resultset.all()

    def get_last_active(self, parent_id: int, content_type: str, workspace: Workspace=None, limit=10) -> [Content]:
        assert parent_id is None or isinstance(parent_id, int) # DYN_REMOVE
        assert content_type is not None# DYN_REMOVE
        assert isinstance(content_type, str) # DYN_REMOVE

        resultset = self._base_query(workspace).order_by(desc(Content.updated))

        if content_type!=ContentType.Any:
            resultset = resultset.filter(Content.type==content_type)

        if parent_id:
            resultset = resultset.filter(Content.parent_id==parent_id)

        result = []
        for item in resultset:
            new_item = None
            if ContentType.Comment == item.type:
                new_item = item.parent
            else:
                new_item = item

            # INFO - D.A. - 2015-05-20
            # We do not want to show only one item if the last 10 items are
            # comments about one thread for example
            if new_item not in result:
                result.append(new_item)

            if len(result) >= limit:
                break

        return result

    def get_last_unread(self, parent_id: int, content_type: str,
                        workspace: Workspace=None, limit=10) -> [Content]:
        assert parent_id is None or isinstance(parent_id, int) # DYN_REMOVE
        assert content_type is not None# DYN_REMOVE
        assert isinstance(content_type, str) # DYN_REMOVE

        read_revision_ids = DBSession.query(RevisionReadStatus.revision_id) \
            .filter(RevisionReadStatus.user_id==self._user_id)

        not_read_revisions = self._revisions_base_query(workspace) \
            .filter(~ContentRevisionRO.revision_id.in_(read_revision_ids)) \
            .subquery()

        not_read_content_ids = DBSession.query(
            distinct(not_read_revisions.c.content_id)).all()

        not_read_contents = self._base_query(workspace) \
            .filter(Content.content_id.in_(not_read_content_ids)) \
            .order_by(desc(Content.updated))

        if content_type != ContentType.Any:
            not_read_contents = not_read_contents.filter(
                Content.type==content_type)
        else:
            not_read_contents = not_read_contents.filter(
                Content.type!=ContentType.Folder)

        if parent_id:
            not_read_contents = not_read_contents.filter(
                Content.parent_id==parent_id)

        result = []
        for item in not_read_contents:
            new_item = None
            if ContentType.Comment == item.type:
                new_item = item.parent
            else:
                new_item = item

            # INFO - D.A. - 2015-05-20
            # We do not want to show only one item if the last 10 items are
            # comments about one thread for example
            if new_item not in result:
                result.append(new_item)

            if len(result) >= limit:
                break

        return result

    def set_allowed_content(self, folder: Content, allowed_content_dict:dict):
        """
        :param folder: the given folder instance
        :param allowed_content_dict: must be something like this:
            dict(
                folder = True
                thread = True,
                file = False,
                page = True
            )
        :return:
        """
        properties = dict(allowed_content = allowed_content_dict)
        folder.properties = properties

    def set_status(self, content: Content, new_status: str):
        if new_status in ContentStatus.allowed_values():
            content.status = new_status
            content.revision_type = ActionDescription.STATUS_UPDATE
        else:
            raise ValueError('The given value {} is not allowed'.format(new_status))

    def move(self, item: Content,
             new_parent: Content,
             must_stay_in_same_workspace:bool=True,
             new_workspace:Workspace=None):
        if must_stay_in_same_workspace:
            if new_parent and new_parent.workspace_id != item.workspace_id:
                raise ValueError('the item should stay in the same workspace')

        item.parent = new_parent
        if new_parent:
            item.workspace = new_parent.workspace
        elif new_workspace:
            item.workspace = new_workspace

        item.revision_type = ActionDescription.MOVE

    def move_recursively(self, item: Content,
                         new_parent: Content, new_workspace: Workspace):
        self.move(item, new_parent, False, new_workspace)
        self.save(item, do_notify=False)

        for child in item.children:
            self.move_recursively(child, item, new_workspace)
        return

    def update_content(self, item: Content, new_label: str, new_content: str=None) -> Content:
        if item.label==new_label and item.description==new_content:
            raise SameValueError(_('The content did not changed'))
        item.owner = self._user
        item.label = new_label
        item.description = new_content if new_content else item.description # TODO: convert urls into links
        item.revision_type = ActionDescription.EDITION
        return item

    def update_file_data(self, item: Content, new_filename: str, new_mimetype: str, new_file_content) -> Content:
        item.owner = self._user
        item.file_name = new_filename
        item.file_mimetype = new_mimetype
        item.file_content = new_file_content
        item.revision_type = ActionDescription.REVISION
        return item

    def archive(self, content: Content):
        content.owner = self._user
        content.is_archived = True
        content.revision_type = ActionDescription.ARCHIVING

    def unarchive(self, content: Content):
        content.owner = self._user
        content.is_archived = False
        content.revision_type = ActionDescription.UNARCHIVING


    def delete(self, content: Content):
        content.owner = self._user
        content.is_deleted = True
        content.revision_type = ActionDescription.DELETION

    def undelete(self, content: Content):
        content.owner = self._user
        content.is_deleted = False
        content.revision_type = ActionDescription.UNDELETION

    def mark_read(self, content: Content,
                  read_datetime: datetime=None,
                  do_flush: bool=True, recursive: bool=True) -> Content:

        assert self._user
        assert content

        # The algorithm is:
        # 1. define the read datetime
        # 2. update all revisions related to current Content
        # 3. do the same for all child revisions
        #    (ie parent_id is content_id of current content)

        if not read_datetime:
            read_datetime = datetime.datetime.now()

        viewed_revisions = DBSession.query(ContentRevisionRO) \
            .filter(ContentRevisionRO.content_id==content.content_id).all()

        for revision in viewed_revisions:
            revision.read_by[self._user] = read_datetime

        if recursive:
            # mark read :
            # - all children
            # - parent stuff (if you mark a comment as read,
            #                 then you have seen the parent)
            # - parent comments
            for child in content.get_valid_children():
                self.mark_read(child, read_datetime=read_datetime,
                               do_flush=False)

            if ContentType.Comment == content.type:
                self.mark_read(content.parent, read_datetime=read_datetime,
                               do_flush=False, recursive=False)
                for comment in content.parent.get_comments():
                    if comment != content:
                        self.mark_read(comment, read_datetime=read_datetime,
                                       do_flush=False, recursive=False)

        if do_flush:
            self.flush()

        return content

    def mark_unread(self, content: Content, do_flush=True) -> Content:
        assert self._user
        assert content

        revisions = DBSession.query(ContentRevisionRO) \
            .filter(ContentRevisionRO.content_id==content.content_id).all()

        for revision in revisions:
            del revision.read_by[self._user]

        for child in content.get_valid_children():
            self.mark_unread(child, do_flush=False)

        if do_flush:
            self.flush()

        return content

    def flush(self):
        DBSession.flush()

    def save(self, content: Content, action_description: str=None, do_flush=True, do_notify=True):
        """
        Save an object, flush the session and set the revision_type property
        :param content:
        :param action_description:
        :return:
        """
        assert action_description is None or action_description in ActionDescription.allowed_values()

        if not action_description:
            # See if the last action has been modified
            if content.revision_type==None or len(get_history(content, 'revision_type'))<=0:
                # The action has not been modified, so we set it to default edition
                action_description = ActionDescription.EDITION

        if action_description:
            content.revision_type = action_description

        if do_flush:
            # INFO - 2015-09-03 - D.A.
            # There are 2 flush because of the use
            # of triggers for content creation
            #
            # (when creating a content, actually this is an insert of a new
            # revision in content_revisions ; so the mark_read operation need
            # to get full real data from database before to be prepared.

            DBSession.add(content)
            DBSession.flush()

            # TODO - 2015-09-03 - D.A. - Do not use triggers
            # We should create a new ContentRevisionRO object instead of Content
            # This would help managing view/not viewed status
            self.mark_read(content, do_flush=True)

        if do_notify:
            self.do_notify(content)

    def do_notify(self, content: Content):
        """
        Allow to force notification for a given content. By default, it is
        called during the .save() operation
        :param content:
        :return:
        """
        NotifierFactory.create(self._user).notify_content_update(content)

    def get_keywords(self, search_string, search_string_separators=None) -> [str]:
        """
        :param search_string: a list of coma-separated keywords
        :return: a list of str (each keyword = 1 entry
        """

        search_string_separators = search_string_separators or ContentApi.SEARCH_SEPARATORS

        keywords = []
        if search_string:
            keywords = [keyword.strip() for keyword in re.split(search_string_separators, search_string)]

        return keywords

    def search(self, keywords: [str]) -> sqlalchemy.orm.query.Query:
        """
        :return: a sorted list of Content items
        """

        if len(keywords)<=0:
            return None

        filter_group_label = list(Content.label.ilike('%{}%'.format(keyword)) for keyword in keywords)
        filter_group_desc = list(Content.description.ilike('%{}%'.format(keyword)) for keyword in keywords)
        title_keyworded_items = self._hard_filtered_base_query().\
            filter(or_(*(filter_group_label+filter_group_desc))).\
            options(joinedload('children')).\
            options(joinedload('parent'))

        return title_keyworded_items

    def get_all_types(self) -> [ContentType]:
        labels = ContentType.all()
        content_types = []
        for label in labels:
            content_types.append(ContentType(label))

        return ContentType.sorted(content_types)

