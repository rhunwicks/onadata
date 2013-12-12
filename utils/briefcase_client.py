import os
import time
import math
import logging
import mimetypes
import requests
from requests.auth import HTTPDigestAuth
from urlparse import urljoin
from xml.parsers.expat import ExpatError

from cStringIO import StringIO
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import InMemoryUploadedFile

from odk_logger.xform_instance_parser import clean_and_parse_xml
from utils.logger_tools import publish_xml_form, publish_form, \
    create_instance


def django_file(file_obj, field_name, content_type):
    return InMemoryUploadedFile(
        file=file_obj,
        field_name=field_name,
        name=file_obj.name,
        content_type=content_type,
        size=file_obj.size,
        charset=None
    )


class BriefcaseClient(object):
    def __init__(self, url, username, password, user):
        self.url = url
        self.user = user
        self.auth = HTTPDigestAuth(username, password)
        self.form_list_url = urljoin(self.url, 'formList')
        self.submission_list_url = urljoin(self.url, 'view/submissionList')
        self.download_submission_url = urljoin(self.url,
                                               'view/downloadSubmission')
        self.forms_path = os.path.join(
            self.user.username, 'briefcase', 'forms')
        self.resumption_cursor = 0
        self.logger = logging.getLogger('console_logger')

    def download_xforms(self, include_instances=False):
        # fetch formList
        response = requests.get(self.form_list_url, auth=self.auth)
        self.logger.debug('Successfull fetched %s.' % self.form_list_url)
        xmlDoc = clean_and_parse_xml(response.content)
        forms = []
        for childNode in xmlDoc.childNodes:
            if childNode.nodeName == 'xforms':
                for xformNode in childNode.childNodes:
                    if xformNode.nodeName == 'xform':
                        form_id = xformNode.getElementsByTagName('formID')[0]
                        id_string = form_id.childNodes[0].nodeValue
                        form_name = xformNode.getElementsByTagName('name')[0]
                        name = form_name.childNodes[0].nodeValue
                        if name.startswith('Crowd/'):
                            # skip crowdforms: very formhub specific
                            continue
                        d = xformNode.getElementsByTagName('downloadUrl')[0]
                        download_url = d.childNodes[0].nodeValue
                        m = xformNode.getElementsByTagName('manifestUrl')[0]
                        manifest_url = m.childNodes[0].nodeValue
                        forms.append((id_string, download_url, manifest_url))
        # download each xform
        if forms:
            for id_string, download_url, manifest_url in forms:
                form_path = os.path.join(
                    self.forms_path, id_string, '%s.xml' % id_string)
                if not default_storage.exists(form_path):
                    form_res = requests.get(download_url, auth=self.auth)
                    content = ContentFile(form_res.content.strip())
                    default_storage.save(form_path, content)
                else:
                    form_res = default_storage.open(form_path)
                    content = form_res.read()
                self.logger.debug("Fetched %s." % download_url)
                manifest_res = requests.get(manifest_url, auth=self.auth)
                try:
                    manifest_doc = clean_and_parse_xml(manifest_res.content)
                except ExpatError:
                    continue
                manifest_path = os.path.join(
                    self.forms_path, id_string, 'form-media')
                self.logger.debug("Downloading media files for %s" % id_string)
                self.download_media_files(manifest_doc, manifest_path)
                if include_instances:
                    self.logger.debug("Downloading submissions for %s" %
                                      id_string)
                    self.download_instances(id_string)
                    self.logger.debug("Done downloading submissions for %s" %
                                      id_string)

    def download_media_files(self, xml_doc, media_path, num_retries=3):
        @retry(num_retries)
        def _download(self, url):
            self._current_response = None
            # S3 redirects, avoid using formhub digest on S3
            head_response = requests.head(url, auth=self.auth)
            if head_response.status_code == 302:
                url = head_response.headers.get('location')
            response = requests.get(url)
            success = response.status_code == 200
            self._current_response = response
            return success

        for media_node in xml_doc.getElementsByTagName('mediaFile'):
            filename_node = media_node.getElementsByTagName('filename')
            url_node = media_node.getElementsByTagName('downloadUrl')
            if filename_node and url_node:
                filename = filename_node[0].childNodes[0].nodeValue
                path = os.path.join(media_path, filename)
                if default_storage.exists(path):
                    continue
                download_url = url_node[0].childNodes[0].nodeValue
                if _download(self, download_url):
                    download_res = self._current_response
                    media_content = ContentFile(download_res.content)
                    default_storage.save(path, media_content)
                    self.logger.debug("Fetched %s." % filename)
                else:
                    self.logger.error("Failed to fetch %s." % filename)

    def download_instances(self, form_id, cursor=0, num_entries=100):
        response = requests.get(self.submission_list_url, auth=self.auth,
                                params={'formId': form_id,
                                        'numEntries': num_entries,
                                        'cursor': cursor})
        self.logger.debug("Fetching %s formId: %s, cursor: %s" %
                         (self.submission_list_url, form_id, cursor))
        try:
            xml_doc = clean_and_parse_xml(response.content)
        except ExpatError:
            return
        instances = []
        for child_node in xml_doc.childNodes:
            if child_node.nodeName == 'idChunk':
                for id_node in child_node.getElementsByTagName('id'):
                    if id_node.childNodes:
                        instance_id = id_node.childNodes[0].nodeValue
                        instances.append(instance_id)
        path = os.path.join(self.forms_path, form_id, 'instances')
        for uuid in instances:
            self.logger.debug("Fetching %s %s submission" % (uuid, form_id))
            form_str = u'%(formId)s[@version=null and @uiVersion=null]/'\
                u'%(formId)s[@key=%(instanceId)s]' % {
                    'formId': form_id,
                    'instanceId': uuid
                }
            instance_path = os.path.join(path, uuid.replace(':', ''),
                                         'submission.xml')
            if not default_storage.exists(instance_path):
                instance_res = requests.get(self.download_submission_url,
                                            auth=self.auth,
                                            params={'formId': form_str})
                content = instance_res.content.strip()
                default_storage.save(instance_path, ContentFile(content))
            else:
                instance_res = default_storage.open(instance_path)
                content = instance_res.read()
            try:
                instance_doc = clean_and_parse_xml(content)
            except ExpatError:
                continue
            media_path = os.path.join(path, uuid.replace(':', ''))
            self.download_media_files(instance_doc, media_path)
            self.logger.debug("Fetched %s %s submission" % (form_id, uuid))
        if xml_doc.getElementsByTagName('resumptionCursor'):
            rs_node = xml_doc.getElementsByTagName('resumptionCursor')[0]
            cursor = rs_node.childNodes[0].nodeValue
            if self.resumption_cursor != cursor:
                self.resumption_cursor = cursor
                self.download_instances(form_id, cursor)

    def _upload_xform(self, path, file_name):
        class PublishXForm(object):
            def __init__(self, xml_file, user):
                self.xml_file = xml_file
                self.user = user

            def publish_xform(self):
                return publish_xml_form(self.xml_file, self.user)
        xml_file = default_storage.open(path)
        xml_file.name = file_name
        k = PublishXForm(xml_file, self.user)
        return publish_form(k.publish_xform)

    def _upload_instances(self, path):
        instances = []
        dirs, not_in_use = default_storage.listdir(path)
        for instance_dir in dirs:
            instance_dir_path = os.path.join(path, instance_dir)
            i_dirs, files = default_storage.listdir(instance_dir_path)
            xml_file = None
            attachments = []
            if 'submission.xml' in files:
                file_obj = default_storage.open(
                    os.path.join(instance_dir_path, 'submission.xml'))
                xml_file = file_obj
            if xml_file:
                try:
                    try:
                        xml_doc = clean_and_parse_xml(xml_file.read())
                    except ExpatError:
                        continue
                    xml = StringIO()
                    de_node = xml_doc.documentElement
                    for node in de_node.firstChild.childNodes:
                        xml.write(node.toxml())
                    new_xml_file = ContentFile(xml.getvalue())
                    new_xml_file.content_type = 'text/xml'
                    xml.close()
                    for attach in de_node.getElementsByTagName('mediaFile'):
                        filename_node = attach.getElementsByTagName('filename')
                        filename = filename_node[0].childNodes[0].nodeValue
                        if filename in files:
                            file_obj = default_storage.open(
                                os.path.join(instance_dir_path, filename))
                            mimetype, encoding = mimetypes.guess_type(
                                file_obj.name)
                            media_obj = django_file(
                                file_obj, 'media_files[]', mimetype)
                            attachments.append(media_obj)
                    instance = create_instance(
                        self.user.username, new_xml_file, attachments)
                except Exception:
                    pass
                else:
                    instances.append(instance)
        return len(instances)

    def push(self):
        dirs, files = default_storage.listdir(self.forms_path)
        for form_dir in dirs:
            dir_path = os.path.join(self.forms_path, form_dir)
            form_dirs, form_files = default_storage.listdir(dir_path)
            form_xml = '%s.xml' % form_dir
            if form_xml in form_files:
                form_xml_path = os.path.join(dir_path, form_xml)
                x = self._upload_xform(form_xml_path, form_xml)
                if isinstance(x, dict):
                    self.logger.error("Failed to publish %s" % form_dir)
                else:
                    self.logger.debug("Successfully published %s" % form_dir)
            if 'instances' in form_dirs:
                self.logger.debug("Uploading instances")
                c = self._upload_instances(os.path.join(dir_path, 'instances'))
                self.logger.debug("Published %d instances for %s" %
                                  (c, form_dir))


def retry(tries, delay=3, backoff=2):
    '''
    Adapted from code found here:
        http://wiki.python.org/moin/PythonDecoratorLibrary#Retry

    Retries a function or method until it returns True.

    *delay* sets the initial delay in seconds, and *backoff* sets the
    factor by which the delay should lengthen after each failure.
    *backoff* must be greater than 1, or else it isn't really a backoff.
    *tries* must be at least 0, and *delay* greater than 0.
    '''

    if backoff <= 1:  # pragma: no cover
        raise ValueError("backoff must be greater than 1")

    tries = math.floor(tries)
    if tries < 0:  # pragma: no cover
        raise ValueError("tries must be 0 or greater")

    if delay <= 0:  # pragma: no cover
        raise ValueError("delay must be greater than 0")

    def decorator_retry(func):
        def function_retry(self, *args, **kwargs):
            mtries, mdelay = tries, delay
            result = func(self, *args, **kwargs)
            while mtries > 0:
                if result:
                    return result
                mtries -= 1
                time.sleep(mdelay)
                mdelay *= backoff
                result = func(self, *args, **kwargs)
            return False

        return function_retry
    return decorator_retry
