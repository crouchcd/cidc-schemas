import re
import json
import os
import copy
import uuid
from typing import Union
import jsonschema
from deepdiff import grep
import datetime
from jsonmerge import merge, Merger
from collections import namedtuple
from jsonpointer import JsonPointer, JsonPointerException, resolve_pointer

from cidc_schemas.json_validation import load_and_validate_schema
from cidc_schemas.template import Template
from cidc_schemas.template_writer import RowType
from cidc_schemas.template_reader import XlTemplateReader

from cidc_schemas.constants import SCHEMA_DIR, TEMPLATE_DIR


def _set_val(
        pointer: str, 
        val: object, 
        context: dict, 
        root: Union[dict, None] = None, 
        context_pointer: Union[str, None] = None, 
        verb=False):
    """
    This function given a *pointer* (jsonpointer RFC 6901 or relative json pointer)
    to a property in a python object, sets the supplied value
    in-place in the *context* object within *root* object.

    The object we are adding data to is *root*. The object
    may or may not have any of the intermediate structure
    to fully insert the desired property.

    For example: consider 
    pointer = "0/prop1/prop2"
    val = {"more": "props"}
    context = {"Pid": 1}
    root = {"participants": [context]}
    context_pointer = "/participants/0"
    
   
    First we see an `0` in pointer which denotes update 
    should be within context. So no need to jump higher than context.
    
    So we truncate path to = "prop1/prop2"

    We see it's a string, so we know we are entering object's *prop1* as a property:
        {
            "participants": [{
                "prop1": ...
            }]
        }
    It's our whole Trial object, and ... here denotes our current descend.

    Now we truncate path one step further to = "prop2" 
    Go down there and set `val={"more": "props"}` :
        {
            "participants": [{
                "prop1": {
                    "prop2": {"more": "props"}
                }
            }]
        }
    While context is a sub-part of that:
            {
                "prop1": {
                    "prop2": {"more": "props"}
                }
            }
    

    Args:
        pointer: relative jsonpointer to the property being set within current `context`
        val: the value being set
        context: the python object relatively to which val is being set
        root: the whole python object being constructed, contains `context`
        context_pointer: jsonpointer of `context` within `root`. Needed to jump up.
        verb: indicates if debug logic should be printed.

    Returns:
       Nothing
    """

    #fill defaults 
    root = context if root is None else root
    context_pointer = "/" if root is None else context_pointer
    

    # first we need to convert pointer to an absolute one
    # if it was a relative one (https://tools.ietf.org/id/draft-handrews-relative-json-pointer-00.html)
    if pointer.startswith('/'):
        jpoint = JsonPointer(pointer)
        doc = context

    else:
        # parse "relative" jumps up
        jumpups, rem_pointer = pointer.split('/',1)
        jumpups = int(jumpups.rstrip('#'))
        # check that we don't have to jump up more than we dived in already 
        assert jumpups <= context_pointer.rstrip('/').count('/'), \
            f"Can't set value for pointer {pointer} - to many jumps up from current."

        # and we'll go down remaining part of `pointer` from there
        jpoint = JsonPointer('/'+rem_pointer)
        if jumpups > 0:
            # new context pointer 
            higher_context_pointer = '/'.join(context_pointer.strip('/').split('/')[:-1*jumpups])
            # making jumps up, by going down context_pointer but no all the way down
            if higher_context_pointer == '':
                doc = root
                assert len(jpoint.parts) > 0, f"Can't update root object (pointer {pointer})"
            else: 
                doc = resolve_pointer(root, '/'+higher_context_pointer)
        else:
            doc = context


    # then we update it
    for i, part in enumerate(jpoint.parts[:-1]):

        try:
            doc = jpoint.walk(doc, part)

        except (JsonPointerException, IndexError) as e:
            # means that there isn't needed sub-object in place
            # so create one

            # look ahead to figure out a proper type that needs to be created
            if i+1 == len(jpoint.parts):
                raise Exception(f"Can't determine how to set value in {pointer!r}")

            next_part = jpoint.parts[i+1]
            
            typed_part = jpoint.get_part(doc, part)

            # `next_part` looks like array index like"[0]" or "-" (RFC 6901)
            if next_part == "-" or jpoint._RE_ARRAY_INDEX.match(str(next_part)):
                # so create array
                next_thing = []
            # or just dict as default
            else:
                next_thing = {}
            
            if part == "-":
                doc.append(next_thing)
            else:
                try:
                    doc[typed_part] = next_thing          
                # if it's an empty array - we get an error 
                # when we try to paste to [0] index,
                # so just append then
                except IndexError:
                    doc.append(next_thing)

            # now we `walk` it again - this time should be OK
            doc = jpoint.walk(doc, part)

    last_part = jpoint.parts[-1]
    if last_part == '-':
        doc.append(val)
        return

    typed_last_part = jpoint.get_part(doc, last_part)
    
    # now we update it with val
    try:
        doc[typed_last_part] = val          
    # if it's an empty array - we get an error 
    # when we try to paste to [0] index,
    # so just append then
    except IndexError:
        assert len(doc) == typed_last_part, f"Can't set value in {pointer!r}"
        doc.append(val)



def _get_recursively(search_dict, field):
    """
    Takes a dict with nested lists and dicts,
    and searches all dicts for a key of the field
    provided.
    """
    fields_found = []

    for key, value in search_dict.items():

        if key == field:
            fields_found.append(value)

        elif isinstance(value, dict):
            results = _get_recursively(value, field)
            for result in results:
                fields_found.append(result)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    more_results = _get_recursively(item, field)
                    for another_result in more_results:
                        fields_found.append(another_result)

    return fields_found


def _process_property(
        row: list,
        key_lu: dict,
        data_obj: dict,
        root_obj: Union[None, dict] = None,
        data_obj_pointer: Union[None, str] = None,
        verb: bool = False) -> dict:
    """
    Takes a single property (key, val) from spreadsheet, determines
    where it needs to go in the final object, then inserts it.

    Args:
        row: array with two fields, key-val
        key_lu: dictionary to translate from template naming to json-schema
                property names
        data_obj: dictionary we are building to represent data
        root_obj: root dictionary we are building to represent data, 
                  that holds 'data_obj' within 'data_obj_pointer'
        data_obj_pointer: pointer of 'data_obj' within 'root_obj'.
                          this will allow to process relative json-pointer properties
                          to jump out of data_object
        verb: boolean indicating verbosity

    Returns:
        TBD

    """

    # simplify
    key = row[0]
    raw_val = row[1]

    if verb:
        print(f"processing property {key!r} - {raw_val!r}")
    # coerce value
    field_def = key_lu[key.lower()]
    if verb:
        print(f'found def {field_def}')
    
    val = field_def['coerce'](raw_val)

    # or set/update value in-place in data_obj dictionary 
    pointer = field_def['merge_pointer']
    if field_def.get('is_artifact'):
        pointer+='/upload_placeholder'

    try:
        _set_val(pointer, val, data_obj, root_obj, data_obj_pointer, verb=verb)
    except Exception as e:
        raise Exception(e)

    if verb:
        print(f'current {data_obj}')

    if field_def.get('is_artifact'):

        if verb:
            print(f'collecting local_file_path {field_def}')

        # setup the base path
        gs_key = ""# _get_recursively(data_obj, "lead_organization_study_id")[0]
        gs_key = f'{gs_key}/{_get_recursively(data_obj, "cimac_participant_id")[0]}'
        gs_key = f'{gs_key}/{_get_recursively(data_obj, "cimac_sample_id")[0]}'
        gs_key = f'{gs_key}/{_get_recursively(data_obj, "cimac_aliquot_id")[0]}'

        artifact_field_name = field_def['merge_pointer'].split('/')[-1]
        gs_key = f'{gs_key}/assay_hint/{artifact_field_name}'

        # return local_path entry
        res = {
            "template_key": key,
            "local_path": raw_val,
            "field_def": field_def,
            "gs_key": gs_key
        }
        if field_def.get('is_artifact'):
            res['/upload_placeholder'] = val
        return res
    
def prismify(xlsx_path: str, template_path: str, assay_hint: str = "", verb: bool = False) -> (dict, dict):
    """
    Converts excel file to json object. It also identifies local files
    which need to uploaded to a google bucket and provides some logic
    to help build the bucket url.

    e.g. file list
    [
        {
            'local_path': '/path/to/fwd.fastq',
            'gs_key': '10021/Patient_1/sample_1/aliquot_1/wes_forward.fastq'
        }
    ]


    Args:
        xlsx_path: file on file system to excel file.
        template_path: path on file system relative to schema root of the
                        temaplate

        assay_hint: string used to help idnetify properties in template. Must
                    be the the root of the template filename i.e.
                    wes_template.json would be wes.
        verb: boolean indicating verbosity

    Returns:
        (tuple):
            arg1: clinical trial object with data parsed from spreadsheet
            arg2: list of objects which describe each file identified.
    """

    # data rows will require a unique identifier
    if not assay_hint == "wes":
        raise NotImplementedError(f'{assay_hint} is not supported yet, only WES is supported.')

    
    # get the root CT schema
    root_ct_schema = load_and_validate_schema("clinical_trial.json")
    # create the result CT dictionary
    root_ct_obj = {'_this': 'root'}
    # and merger for it
    root_merger = Merger(root_ct_schema)
    # and where to collect all local file refs
    local_file_paths = []

    # read the excel file
    xslx = XlTemplateReader.from_excel(xlsx_path)
    # get corr xsls schema
    xlsx_template = Template.from_json(template_path)
    xslx.validate(xlsx_template)

    # loop over spreadsheet worksheets
    for ws_name, ws in xslx.grouped_rows.items():
        if verb:
            print(f'next worksheet {ws_name}')

        templ_ws = xlsx_template.template_schema['properties']['worksheets'][ws_name]
        preamble_object_schema = load_and_validate_schema(templ_ws['prism_preamble_object_schema'])
        preamble_merger = Merger(preamble_object_schema)
        preamble_object_pointer = templ_ws['prism_preamble_object_pointer']
        data_object_pointer = templ_ws['prism_data_object_pointer']

        # creating preamble obj 
        preamble_obj = {'_this': f'preamble_obj_{ws_name}'}
        
        # get headers
        headers = ws[RowType.HEADER][0]

        # get the data
        data = ws[RowType.DATA]
        # for row in data:
        for i, row in enumerate(data):

            # creating data obj 
            data_obj = {'_this': f"data_obj_{i}"}
            copy_of_preamble = copy.deepcopy(preamble_obj)
            copy_of_preamble['_this'] = f"copy_of_preamble_obj_{ws_name}_{i}" 
            _set_val(data_object_pointer, data_obj, copy_of_preamble, verb=verb)

            # create dictionary per row
            for key, val in zip(headers, row):
                
                # get corr xsls schema type 
                new_file = _process_property([key, val], xlsx_template.key_lu, data_obj, copy_of_preamble, data_object_pointer, verb)
                if new_file:
                    local_file_paths.append(new_file)

            preamble_obj = preamble_merger.merge(preamble_obj, copy_of_preamble)
        

        _set_val(preamble_object_pointer, preamble_obj, root_ct_obj, verb=verb)
        # Compare preamble rows
        for row in ws[RowType.PREAMBLE]:

            # TODO maybe use preamble merger as well?
            # process this property
            new_file = _process_property(row, xlsx_template.key_lu, preamble_obj, root_ct_obj, preamble_object_pointer, verb=verb)
            if new_file:
                local_file_paths.append(new_file)

    if verb:
        print({k:len(v) for k,v in root_ct_obj['assays'].items()})

    # assert False and i<1, ([r['files'] for r in preamble_obj.get("records",[])])
    # return the object.
    return root_ct_obj, local_file_paths


def _get_path(ct: dict, key: str) -> str:
    """
    find the path to the given key in the dictionary

    Args:
        ct: clinical_trial object to be modified
        key: the identifier we are looking for in the dictionary

    Returns:
        arg1: string describing the location of the key
    """

    # first look for key as is
    ds1 = ct | grep(key, match_string=True)
    count1 = 0
    if 'matched_values' in ds1:
        count1 = len(ds1['matched_values'])

    # the hack fails if both work... probably need to deal with this
    if count1 == 0:
        raise NotImplementedError(f"key: {key} not found in dictionary")

    # get the keypath
    return ds1['matched_values'].pop()


def _get_source(ct: dict, key: str, slice=None) -> dict:
    """
    extract the object in the dicitionary specified by
    the supplied key (or one of its parents.)

    Args:
        ct: clinical_trial object to be searched
        key: the identifier we are looking for in the dictionary,
        slice: how many levels down we want to go, usually will be 
            negative 

    Returns:
        arg1: string describing the location of the key
    """

    # tokenize.
    key = key.replace("root", "").replace("'", "")
    tokens = re.findall(r"\[(.*?)\]", key)

    tokens = tokens[0:slice]
    
    # keep getting based on the key.
    cur_obj = ct
    for token in tokens:
        try:
            token = int(token)
        except ValueError:
            pass

        cur_obj = cur_obj[token]

    return cur_obj


def _merge_artifact_wes(
    ct: dict,
    object_url: str,
    file_size_bytes: int,
    uploaded_timestamp: str,
    md5_hash: str
) -> (dict, dict):
    """
    create and merge an artifact into the WES assay metadata.
    The artifacts currently supported are only the input
    fastq files and read mapping group file.

    Args:
        ct: clinical_trial object to be searched
        object_url: the gs url pointing to the object being added
        file_size_bytes: integer specifying the numebr of bytes in the file
        uploaded_timestamp: time stamp associated with this object
        md5_hash: hash of the uploaded object, usually provided by
                    object storage

    """

    # replace gs prfix if exists.
    wes_object = _split_wes_url(object_url)

    
    # create the artifact
    artifact = {
        "artifact_category": "Assay Artifact from CIMAC",
        "object_url": object_url,
        "file_name": wes_object.file_name,
        "file_size_bytes": 1,
        "md5_hash": md5_hash,
        "uploaded_timestamp": uploaded_timestamp
    }

    
    all_WESes = ct.get('assays',{}).get('wes',[])

    ## TODO use jsonpointer maybe? 
    # get the wes record by aliquot_id.
    record_path = _get_path(all_WESes, wes_object.cimac_aliquot_id)

    # slice=-1 is for go one level up from 'cimac_aliquot_id' field to it's parent record 
    record_obj = _get_source(all_WESes, record_path, slice=-1)

    assert record_obj['cimac_aliquot_id'] == wes_object.cimac_aliquot_id
    assert record_obj['cimac_sample_id'] == wes_object.cimac_sample_id
    assert record_obj['cimac_participant_id'] == wes_object.cimac_participant_id

    # modify inplace
    # TODO maybe use merger(template['prism_preamble_object_schema']) 
    ## as we don't `copy.deepcopy(ct)`+`merge` - just return it
    record_obj['files'][wes_object.file_name] = artifact

    ## we skip that because we didn't check `ct` on start
    # validator.validate(ct)

    return ct


WesFileUrlParts = namedtuple("FileUrlParts", ["lead_organization_study_id", "cimac_participant_id", \
        "cimac_sample_id", "cimac_aliquot_id", "assay", "file_name"]) 

def _split_wes_url(obj_url: str) -> WesFileUrlParts:
    
    # parse the url to get key identifiers
    tokens = obj_url.split("/")
    assert len(tokens) == len(WesFileUrlParts._fields), f"bad GCS url {obj_url}"

    return WesFileUrlParts(*tokens)


def merge_artifact(
    ct: dict,
    assay: str,
    object_url: str,
    file_size_bytes: int,
    uploaded_timestamp: str,
    md5_hash: str
) -> (dict, dict):
    """
    create and merge an artifact into the metadata blob
    for a clinical trial. The merging process is automatically
    determined by inspecting the gs url path.

    Args:
        ct: clinical_trial object to be searched
        object_url: the gs url pointing to the object being added
        file_size_bytes: integer specifying the number of bytes in the file
        uploaded_timestamp: time stamp associated with this object
        md5_hash: hash of the uploaded object, usually provided by
                    object storage

    """

   
    if assay == "wes":
        new_ct = _merge_artifact_wes(
            ct,
            object_url,
            file_size_bytes,
            uploaded_timestamp,
            md5_hash
        )
    else:
        raise NotImplementedError(
            f'the following assay is not supported: {assay}')

    # return new object and the artifact that was merged
    return new_ct


def merge_clinical_trial_metadata(patch: dict, target: dict) -> dict:
    """
    merges two clinical trial metadata objects together

    Args:
        patch: the metadata object to add
        target: the existing metadata object

    Returns:
        arg1: the merged metadata object
    """

    # merge the copy with the original.
    validator = load_and_validate_schema(
        "clinical_trial.json", return_validator=True)
    schema = validator.schema

    # first we assert both objects are valid
    validator.validate(target)
    validator.validate(patch)

    # next assert the un-mutable fields are equal
    # these fields are required in the schema
    # so previous validation assert they exist
    key_details = ["lead_organization_study_id"]
    for d in key_details:
        if patch.get(d) != target.get(d):
            raise RuntimeError("unable to merge trials with different \
                lead_organization_study_id")

    # merge the two documents
    merger = Merger(schema)
    merged = merger.merge(target, patch)

    # validate this
    validator.validate(merged)

    # now return it
    return merged
