#!/usr/bin/env python
#
# Copyright 2015 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from datetime import datetime, timedelta
from google.appengine.api import taskqueue, urlfetch
from lxml import etree
from urlparse import urljoin, urlparse
import cgi
import json
import logging
import models

################################################################################

def BuildResponse(objects):
    metadata_output = []

    # Resolve the devices
    for obj in objects:
        key_id = None
        url = None
        force = obj.get('force', False)
        valid = True
        siteInfo = None
        rssi = None
        txpower = None

        if 'id' in obj:
            key_id = obj['id']
        elif 'url' in obj:
            key_id = obj['url']
            url = obj['url']
            parsed_url = urlparse(url)
            if parsed_url.scheme != 'http' and parsed_url.scheme != 'https':
                valid = False

        # We need to go and fetch.  We probably want to asyncly fetch.

        try:
            rssi = float(obj['rssi'])
            txpower = float(obj['txpower'])
        except:
            pass

        if valid:
            # Really if we don't have the data we should not return it.
            siteInfo = models.SiteInformation.get_by_id(url)

            if force or siteInfo is None:
                # If we don't have the data or it is older than 5 minutes, fetch.
                siteInfo = FetchAndStoreUrl(siteInfo, url)
            if siteInfo is not None and siteInfo.updated_on < datetime.now() - timedelta(minutes=5):
                # Updated time to make sure we don't request twice.
                siteInfo.put()
                # Add request to queue.
                taskqueue.add(url='/refresh-url', params={'url': url})

        device_data = {};
        if siteInfo is not None:
            device_data['id'] = url
            device_data['url'] = siteInfo.url
            if siteInfo.title is not None:
                device_data['title'] = siteInfo.title
            if siteInfo.description is not None:
                device_data['description'] = siteInfo.description
            if siteInfo.favicon_url is not None:
                device_data['icon'] = siteInfo.favicon_url
            if siteInfo.jsonlds is not None:
                device_data['json-ld'] = json.loads(siteInfo.jsonlds)
        else:
            device_data['id'] = url
            device_data['url'] = url
        device_data['rssi'] = rssi
        device_data['txpower'] = txpower

        metadata_output.append(device_data)

    def ReplaceRssiTxPowerWithPathLossAsRank(device_data):
        try:
            path_loss = device_data['txpower'] - device_data['rssi']
            device_data['rank'] = path_loss
        except:
            # TODO: We could leave rank off, but this makes clients job easier
            device_data['rank'] = 1000.0
        finally:
            del device_data['txpower']
            del device_data['rssi']
        return device_data

    metadata_output = map(ReplaceRssiTxPowerWithPathLossAsRank, RankedResponse(metadata_output))
    return metadata_output

################################################################################

def RankedResponse(metadata_output):
    def ComputeDistance(obj):
        try:
            rssi = float(obj['rssi'])
            txpower = float(obj['txpower'])
            if rssi == 127 or rssi == 128:
                # TODO: What does rssi 127 mean, compared to no value?
                # According to wiki, 127 is MAX and 128 is INVALID.
                # I think we should just leave 127 to calc distance as usual, so it sorts to the end but before the unknowns
                return None
            path_loss = txpower - rssi
            distance = pow(10.0, path_loss - 41) # TODO: Took this from Hoa's patch, but should confirm accuracy
            return distance
        except:
            return None

    def SortByDistanceCmp(a, b):
        dista, distb = ComputeDistance(a), ComputeDistance(b)
        if dista is None and distb is None:
            return 0 # No winner
        if dista is None:
            return -1 # assume b is closer
        if distb is None:
            return 1 # assume a is closer
        return int(dista - distb)

    metadata_output.sort(SortByDistanceCmp)
    return metadata_output

################################################################################

def FetchAndStoreUrl(siteInfo, url):
    # Index the page
    try:
        result = urlfetch.fetch(url, validate_certificate = True)
    except:
        return StoreInvalidUrl(siteInfo, url)

    if result.status_code == 200:
        encoding = GetContentEncoding(result.content)
        final_url = GetExpandedURL(url)
        real_final_url = result.final_url
        if real_final_url is None:
            real_final_url = final_url
        return StoreUrl(siteInfo, url, final_url, real_final_url, result.content, encoding)
    else:
        return StoreInvalidUrl(siteInfo, url)

################################################################################

def GetExpandedURL(url):
    parsed_url = urlparse(url)
    final_url = url
    url_shorteners = ['t.co', 'goo.gl', 'bit.ly', 'j.mp', 'bitly.com',
        'amzn.to', 'fb.com', 'bit.do', 'adf.ly', 'u.to', 'tinyurl.com',
        'buzurl.com', 'yourls.org', 'qr.net']
    url_shorteners_set = set(url_shorteners)
    if parsed_url.netloc in url_shorteners_set and (parsed_url.path != '/' or
        parsed_url.path != ''):
        # expand
        result = urlfetch.fetch(url, method = 'HEAD', follow_redirects = False)
        if result.status_code == 301:
            final_url = result.headers['location']
    return final_url

################################################################################

def GetContentEncoding(content):
    encoding = None
    parser = etree.HTMLParser(encoding='iso-8859-1')
    htmltree = etree.fromstring(content, parser)
    value = htmltree.xpath("//head//meta[@http-equiv='Content-Type']/attribute::content")
    if encoding is None:
        if (len(value) > 0):
            content_type = value[0]
            _, params = cgi.parse_header(content_type)
            if 'charset' in params:
                encoding = params['charset']

    if encoding is None:
        value = htmltree.xpath('//head//meta/attribute::charset')
        if (len(value) > 0):
            encoding = value[0]

    if encoding is None:
        try:
            encoding = 'utf-8'
            u_value = unicode(content, 'utf-8')
        except UnicodeDecodeError:
            encoding = 'iso-8859-1'
            u_value = unicode(content, 'iso-8859-1')

    return encoding

################################################################################

def FlattenString(input):
    input = input.strip()
    input = input.replace('\r', ' ');
    input = input.replace('\n', ' ');
    input = input.replace('\t', ' ');
    input = input.replace('\v', ' ');
    input = input.replace('\f', ' ');
    while '  ' in input:
        input = input.replace('  ', ' ');
    return input

################################################################################

def StoreInvalidUrl(siteInfo, url):
    if siteInfo is None:
        siteInfo = models.SiteInformation.get_or_insert(url, 
            url = url,
            title = None,
            favicon_url = None,
            description = None,
            jsonlds = None)
    else:
        # Don't update if it was already cached.
        siteInfo.put()

    return siteInfo

################################################################################

def StoreUrl(siteInfo, url, final_url, real_final_url, content, encoding):
    title = None
    description = None
    icon = None

    # parse the content
    parser = etree.HTMLParser(encoding=encoding)
    htmltree = etree.fromstring(content, parser)
    value = htmltree.xpath('//head//title/text()');
    if (len(value) > 0):
        title = value[0]
    if title is None:
        value = htmltree.xpath("//head//meta[@property='og:title']/attribute::content");
        if (len(value) > 0):
            title = value[0]
    if title is not None:
        title = FlattenString(title)

    # Try to use <meta name="description" content="...">.
    value = htmltree.xpath("//head//meta[@name='description']/attribute::content")
    if (len(value) > 0):
        description = value[0]
    if description is not None and len(description) == 0:
        description = None
    if description == title:
        description = None

    # Try to use <meta property="og:description" content="...">.
    if description is None:
        value = htmltree.xpath("//head//meta[@property='og:description']/attribute::content")
        description = ' '.join(value)
        if len(description) == 0:
            description = None

    # Try to use <div class="content">...</div>.
    if description is None:
        value = htmltree.xpath("//body//*[@class='content']//*[not(*|self::script|self::style)]/text()")
        description = ' '.join(value)
        if len(description) == 0:
            description = None

    # Try to use <div id="content">...</div>.
    if description is None:
        value = htmltree.xpath("//body//*[@id='content']//*[not(*|self::script|self::style)]/text()")
        description = ' '.join(value)
        if len(description) == 0:
            description = None

    # Fallback on <body>...</body>.
    if description is None:
        value = htmltree.xpath("//body//*[not(*|self::script|self::style)]/text()")
        description = ' '.join(value)
        if len(description) == 0:
            description = None

    # Cleanup.
    if description is not None:
        description = FlattenString(description)
        if len(description) > 500:
            description = description[:500]

    if icon is None:
        value = htmltree.xpath("//head//link[@rel='shortcut icon']/attribute::href");
        if (len(value) > 0):
            icon = value[0]
    if icon is None:
        value = htmltree.xpath("//head//link[@rel='icon']/attribute::href");
        if (len(value) > 0):
            icon = value[0]
    if icon is None:
        value = htmltree.xpath("//head//link[@rel='apple-touch-icon-precomposed']/attribute::href");
        if (len(value) > 0):
            icon = value[0]
    if icon is None:
        value = htmltree.xpath("//head//link[@rel='apple-touch-icon']/attribute::href");
        if (len(value) > 0):
            icon = value[0]
    if icon is None:
        value = htmltree.xpath("//head//meta[@property='og:image']/attribute::content");
        if (len(value) > 0):
            icon = value[0]

    if icon is not None:
        if icon.startswith('./'):
            icon = icon[2:len(icon)]
        icon = urljoin(real_final_url, icon)
    if icon is None:
        icon = urljoin(real_final_url, '/favicon.ico')
    # make sure the icon exists
    try:
        result = urlfetch.fetch(icon, method = 'HEAD')
        if result.status_code != 200:
            icon = None
        else:
            contentType = result.headers['Content-Type']
            if contentType is None:
                icon = None
            elif not contentType.startswith('image/'):
                icon = None
    except:
        s_url = url
        s_final_url = final_url
        s_real_final_url = real_final_url
        s_icon = icon
        if s_url is None:
            s_url = '[none]'
        if s_final_url is None:
            s_final_url = '[none]'
        if s_real_final_url is None:
            s_real_final_url = '[none]'
        if s_icon is None:
            s_icon = '[none]'
        logging.warning('icon error with ' + s_url + ' ' + s_final_url + ' ' + s_real_final_url + ' -> ' + s_icon)
        icon = None

    jsonlds = []
    value = htmltree.xpath("//head//script[@type='application/ld+json']/text()");
    for jsonldtext in value:
        jsonldobject = None
        try:
            jsonldobject = json.loads(jsonldtext) # Data is not sanitised.
        except UnicodeDecodeError:
            jsonldobject = None
        if jsonldobject is not None:
            jsonlds.append(jsonldobject)

    if (len(jsonlds) > 0):
        jsonlds_data = json.dumps(jsonlds);
    else:
        jsonlds_data = None

    if siteInfo is None:
        siteInfo = models.SiteInformation.get_or_insert(url, 
            url = final_url,
            title = title,
            favicon_url = icon,
            description = description,
            jsonlds = jsonlds_data)
    else:
        # update the data because it already exists
        siteInfo.url = final_url
        siteInfo.title = title
        siteInfo.favicon_url = icon
        siteInfo.description = description
        siteInfo.jsonlds = jsonlds_data
        siteInfo.put()

    return siteInfo

################################################################################

