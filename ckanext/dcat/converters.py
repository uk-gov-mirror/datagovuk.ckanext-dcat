import re
import logging

from ckan.lib import helpers as h

log = logging.getLogger(__name__)


class ConvertError(Exception):
    pass


def ckan_format_to_dcat_mimetype(ckan_format):
    formats = h.resource_formats()
    format = formats.get(ckan_format.lower())
    if format:
        return format[0]
    return ckan_format

def check_value_is_a_list_of_strings(key, value):
    if not isinstance(value, (list, tuple)):
        type_ = 'string' if isinstance(value, basestring) else type(value)
        err = '"%s" value must be an array of strings, not: %s' % \
            (key, type_)
        raise ConvertError(err)

def dcat_to_ckan(dcat_dict):

    package_dict = {}

    package_dict['title'] = dcat_dict.get('title')
    if not package_dict['title']:
        raise ConvertError('Dataset does not have a title')
    package_dict['notes'] = dcat_dict.get('description')
    package_dict['url'] = dcat_dict.get('landingPage') or dcat_dict.get('uri')

    package_dict['tags'] = []
    keywords = dcat_dict.get('keyword') or []
    check_value_is_a_list_of_strings('keywords', keywords)
    for keyword in keywords:
        package_dict['tags'].append({'name': keyword})

    package_dict['extras'] = []
    for key in ['issued', 'modified']:
        # NB these dates refer to when the data itself was changed, not the
        # metadata
        package_dict['extras'].append({'key': 'data_{0}'.format(key), 'value': dcat_dict.get(key)})

    # DR: I agree with keeping the URI and dct:identifier separate - the
    # dct:identifier might be some hex rather than a URI. However I'm not sure
    # about calling it 'guid' as the harvest_object.guid defaults to the URI.
    package_dict['extras'].append({'key': 'guid', 'value': dcat_dict.get('identifier')})
    package_dict['extras'].append({'key': 'metadata_uri', 'value': dcat_dict.get('uri')})

    # When harvested, the owner_org will be set according to the harvest
    # source. So the dcat.publisher is really a secondary organization, to
    # store in an extra for reference.
    dcat_publisher = dcat_dict.get('publisher')
    if isinstance(dcat_publisher, basestring):
        package_dict['extras'].append({'key': 'dcat_publisher_name', 'value': dcat_publisher})
    elif isinstance(dcat_publisher, dict):
        if dcat_publisher.get('name'):
            package_dict['extras'].append({'key': 'dcat_publisher_name', 'value': dcat_publisher.get('name')})
        if dcat_publisher.get('uri'):
            package_dict['extras'].append({'key': 'dcat_publisher_uri', 'value': dcat_publisher.get('uri')})
        # it's not normal for a harvester to edit the publisher's email
        # address, so just store this info in an extra
        if dcat_publisher.get('mbox'):
            package_dict['extras'].append({'key': 'dcat_publisher_email', 'value': dcat_publisher.get('mbox')})

    contact_email = dcat_dict.get('contactEmail')
    if contact_email:
        package_dict['extras'].append({'key': 'contact_email', 'value': contact_email})

    # subject is a URI, so although it is similar to a tag, it will need some
    # more work.  It is used to set the theme in DGU.
    subjects = dcat_dict.get('subject')
    if subjects:
        package_dict['extras'].append({'key': 'dcat_subject', 'value': ' '.join(subjects)})

    # The dcat licence URL will need matching to find the equivalent CKAN
    # licence_id if there is one. So alway store it in an extra, and if there
    # is a match, write the license_id.
    dcat_license = dcat_dict.get('license')
    if dcat_license == 'No license provided':
        # Socrata convention
        dcat_license = None
    if dcat_license:
        # Should it be a URL or textual title of the license?
        # NB DCAT gives you a URL, the data.json spec is not clear, and the
        # data.json examples appear to be textual. e.g. "Public Domain":
        # * http://eeoc.gov/data.json (?)
        # * https://nycopendata.socrata.com/data.json (Socrata)
        if dcat_license.startswith('http'):
            package_dict['extras'].append({'key': 'license_url', 'value': dcat_license})
            matched_ckan_license_id = find_license_by_uri(dcat_license)
            if matched_ckan_license_id:
                package_dict['license_id'] = matched_ckan_license_id
        else:
            package_dict['extras'].append({'key': 'license_name', 'value': dcat_license})
            matched_ckan_license_id = find_license_by_title(dcat_license)
            if matched_ckan_license_id:
                package_dict['license_id'] = matched_ckan_license_id
    elif 'licence' in dcat_dict:
        raise ConvertError('Use "license" not "licence"')

    #if dcat_dict.get('isReplacedBy'):
    #    # This means the dataset is obsolete and needs deleting in CKAN.
    #    # This is a suggestion, but not used yet, so is commented out.
    #    import pdb; pdb.set_trace()
    #    package_dict['state'] = 'deleted'

    language_list = dcat_dict.get('language') or []
    check_value_is_a_list_of_strings('language', language_list)
    package_dict['extras'].append({
        'key': 'language',
        'value': ','.join(language_list)
    })

    package_dict['resources'] = []
    for distribution in (dcat_dict.get('distribution') or []):
        add_title_for_socrata(distribution)
        fix_socrata_formats(distribution)
        fix_esri_formats(distribution)
        mimetype = distribution.get('format') or distribution.get('mediaType')
        format = h.unified_resource_format(mimetype) if mimetype else None
        resource = {
            'name': distribution.get('title'),
            'description': distribution.get('description'),
            'url': distribution.get('downloadURL') or distribution.get('accessURL'),
            'format': format,
        }
        if distribution.get('temporal'):
            date, additional_name = \
                distribution_temporal_to_date(distribution['temporal'])
            resource['date'] = date
            if additional_name:
                resource['name'] += ' %s' % additional_name

        if distribution.get('byteSize'):
            try:
                resource['size'] = int(distribution.get('byteSize'))
            except ValueError:
                pass
        package_dict['resources'].append(resource)
    if dcat_dict.get('dataDump'):
        package_dict['resources'].append({
            'name': 'Data dump',
            'description': None,
            'url': dcat_dict.get('dataDump'),
            'format': 'RDF',
            'resource_type': 'file',
        })
    if dcat_dict.get('sparqlEndpoint'):
        package_dict['resources'].append({
            'name': 'SPARQL Endpoint',
            'description': None,
            'url': dcat_dict.get('sparqlEndpoint'),
            'format': 'SPARQL',
            'resource_type': 'api',
        })
    if dcat_dict.get('zippedShapefile'):
        package_dict['resources'].append({
            'name': 'Data as shapefile (zipped)',
            'description': None,
            'url': dcat_dict.get('zippedShapefile'),
            'format': 'SHP',
            'resource_type': 'file',
        })
    # ODC did't want this, but seems the best way to add docs.
    references = dcat_dict.get('references') or []
    check_value_is_a_list_of_strings('references', references)
    for reference in references:
        if isinstance(reference, dict):
            # A dict is outside of POD and have not thought about RDF equivalent
            title = reference.get('title') or 'Reference'
            url = reference.get('url')
            format_ = None
            mimetype = reference.get('format') or reference.get('mediaType')
            if mimetype:
                format_ = h.unified_resource_format(mimetype)
            if not format_:
                format_ = guess_format_from_url(url, default='HTML')
        else:
            title = 'Reference'
            url = reference
            format_ = guess_format_from_url(reference, default='HTML')
        package_dict['resources'].append({
            'name': title,
            'description': None,
            'url': url,
            'format': format_,
            'resource_type': 'documentation',
        })
    if dcat_dict.get('landingPage'):
        package_dict['resources'].append({
            'name': 'Landing page',
            'description': None,
            'url': dcat_dict.get('landingPage'),
            'format': 'HTML',
            'resource_type': 'documentation',
        })

    return package_dict


def ckan_to_dcat(package_dict):

    dcat_dict = {}

    dcat_dict['title'] = package_dict.get('title')
    dcat_dict['description'] = package_dict.get('notes')
    dcat_dict['landingPage'] = package_dict.get('url')

    dcat_dict['keyword'] = []
    for tag in (package_dict.get('tags') or []):
        dcat_dict['keyword'].append(tag['name'])

    dcat_dict['publisher'] = {}

    for extra in (package_dict.get('extras') or []):
        if extra['key'] in ['data_issued', 'data_modified']:
            dcat_dict[extra['key'].replace('data_', '')] = extra['value']

        elif extra['key'] == 'language':
            dcat_dict['language'] = extra['value'].split(',')

        elif extra['key'] == 'dcat_publisher_name':
            dcat_dict['publisher']['name'] = extra['value']

        elif extra['key'] == 'dcat_publisher_email':
            dcat_dict['publisher']['mbox'] = extra['value']

        elif extra['key'] == 'guid':
            dcat_dict['identifier'] = extra['value']

        elif extra['key'] == 'license_url':
            dcat_dict['license'] = extra['value']

    if not dcat_dict['publisher'].get('name') and package_dict.get('maintainer'):
        dcat_dict['publisher']['name'] = package_dict.get('maintainer')
        if package_dict.get('maintainer_email'):
            dcat_dict['publisher']['mbox'] = package_dict.get('maintainer_email')

    dcat_dict['distribution'] = []
    for resource in (package_dict.get('resources') or []):
        distribution = {
            'title': resource.get('name'),
            'description': resource.get('description'),
            'format': ckan_format_to_dcat_mimetype(resource.get('format')),
            'byteSize': resource.get('size'),
            # TODO: downloadURL or accessURL depending on resource type?
            'accessURL': resource.get('url'),
        }
        dcat_dict['distribution'].append(distribution)

    return dcat_dict


def distribution_temporal_to_date(temporal):
    bits = temporal.split('/')
    if len(bits) == 1:
        raise ConvertError('Distribution "temporal" field must have a "/" in it')
    elif len(bits) > 2:
        raise ConvertError('Distribution "temporal" field must only have one "/" in it')
    bits = [bit.strip() for bit in bits]
    try:
        dates = [iso8601_date_to_british(bits[0], can_be_duration=False),
                 iso8601_date_to_british(bits[1], can_be_duration=True)]
    except ValueError, e:
        raise ConvertError(
            'Distribution "temporal" date didn\'t parse: %s "%s". '
            'Check it is in ISO8601 format e.g. "YYYY-MM-DD"' % (e, bit))
    if bits[0] != bits[1]:
        additional_name = '(%s-%s)' % tuple(dates)
    else:
        additional_name = None
    return dates[0], additional_name

def iso8601_date_to_british(date, can_be_duration=True):
    '''
    '2016-01' -> '01/2016'
    If it is an invalid ISO8601 date, it returns ValueError
    If it is a duration it returns the same string
    '''
    # check it parses (ie not out of range)
    import dateutil
    try:
        dateutil.parser.parse(date)
    except (ValueError, TypeError), e:
        if date.startswith('P'):
            # it is a duration
            if can_be_duration:
                return date
            raise ValueError('Cannot have a duration here: "%s"' % date)
        raise ValueError(str(e))  # invalid date
    # reverse direction, strip time, change to slashes
    return '/'.join(re.split('[^\d]', date)[:3][::-1])


def find_license_by_uri(license_uri):
    from ckan import model
    for license in model.Package.get_license_register().values():
        if license.url == license_uri:
            return license.id
    # special cases - OGL has several versions that all map to uk-ogl
    if license_uri.startswith('http://www.nationalarchives.gov.uk/doc/open-government-licence/'):
        return 'uk-ogl'

def find_license_by_title(license_title):
    from ckan import model
    license_title_lower = license_title.lower()
    for license in model.Package.get_license_register().values():
        if license.title.lower() == license_title_lower:
            return license.id

global _socrata_url_regex, _socrata_geo_format_regex
_socrata_url_regex = None
_socrata_geo_format_regex = None


def add_title_for_socrata(distribution):
    '''
    Socrata doesn't give each resource a name/description, so add it based
    on the url
    e.g. https://sandbox.demo.socrata.com/api/views/qcq7-r62w/rows.rdf?accessType=DOWNLOAD
         https://data.bathhacked.org/api/geospatial/tu26-eg7z?method=export&format=KML
    '''
    if distribution.get('description') or distribution.get('title'):
        return
    global _socrata_url_regex
    if not _socrata_url_regex:
        _socrata_url_regex = re.compile('.*/rows\.[^\?]+?accessType=(\w+)')
    match = _socrata_url_regex.match(distribution.get('accessURL', ''))
    if not match:
        distribution['title'] = 'Download'
        return
    accessType = match.groups()[0]
    distribution['title'] = accessType.capitalize()


def fix_socrata_formats(distribution):
    '''
    Socrata has a couple of weird mediaTypes, so fix that

        {"downloadURL": "https://data.bathhacked.org/api/geospatial/t5sn-f4vu?method=export&format=Shapefile",
        "mediaType": "application/zip"},
     should be Shapefile (there is no mimetype)

        {"downloadURL": "https://data.bathhacked.org/api/geospatial/t5sn-f4vu?method=export&format=Original",
        "mediaType": "application/zip"},
     should be '' - depends on what format it was uploaded
    '''
    global _socrata_geo_format_regex
    if not _socrata_geo_format_regex:
        _socrata_geo_format_regex = re.compile('.*/api/geospatial/[^\?/]+?.*format=(\w+)')
    match = _socrata_geo_format_regex.match(distribution.get('downloadURL') or '')
    if not match:
        return
    socrata_format = match.groups()[0]
    if socrata_format == 'Shapefile':
        distribution['format'] = 'Shapefile'
    elif socrata_format == 'Original':
        distribution['mediaType'] = distribution['format'] = ''

def fix_esri_formats(distribution):
    '''
    ESRI has a couple of oddities in its formats:

         "format":"OGC WMS",
         "mediaType":"application/vnd.ogc.wms_xml",
    should be just 'WMS'
    '''
    if distribution.get('format', '').startswith('OGC '):
        distribution['format'] = \
            distribution['format'].replace('OGC ', '')

def guess_format_from_url(url, default=None):
    extension = url.split('.')[-1]
    format_ = h.unified_resource_format(extension)
    if format_ and format_ != extension:
        return format_
    return default
