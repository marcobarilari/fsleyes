#!/usr/bin/env python
#
# fsleyes_parseargs.py - Parsing FSLEyes command line arguments.
#
# Author: Paul McCarthy <pauldmccarthy@gmail.com>
#
"""This module encapsulates the logic for parsing command line arguments which
specify a scene to be displayed in *FSLeyes*.  This logic is shared between
the :mod:`fsleyes` and :mod:`render` tools.  This module
make use of the command line generation features of the :mod:`props` package.


There are a lot of command line arguments made available to the user,
broadly split into the following groups:

 - *Main* arguments control the overall scene display, such as the
   display type (e.g. orthographic or lightbox), the displayed location,
   and whether to show a colour bar. These arguemnts generally correspond
   to properties of the :class:`.SceneOpts`, :class:`.OrthoOpts`,
   :class:`.LightBoxOpts` and :class:`.DisplayContext` classes.


 - *Display* arguments control the display for a single overlay file (e.g.
   a NIFTI1 image), such as interpolation, colour map, etc. These arguments
   correspond to properties of the :class:`.Display` class, and sub-classes
   of :class:`.DisplayOpts`.


This module provides the following functions: 

.. autosummary::
   :nosignatures:

   parseArgs
   applySceneArgs
   generateSceneArgs
   generateOverlayArgs
   applyOverlayArgs


--------------------------
``argparse`` modifications
--------------------------


The ``argparse`` module is quite frustrating to work with for the command
line interface that I want to provide. Therefore, this module modifies
the behaviour of ``argparse.ArgumentParser`` instances (by monkey-patching
instances - not the class itself) such that:

 - Prefix matching (a.k.a. abbreviation) is disabled

 - An error is raised when invalid arguments are passed, rather than
   the program exiting.


------------------------------
Command line parsing procedure
------------------------------


*FSLeyes* command line arguments are processed using the following procedure
 (implemented in the :func:`parseArgs` function):


 1. All overlay paths are identified.

 2. *Main* arguments are separated out from the *display* arguments for every
    overlay.

 3. Main arguments are parsed.

 4. The display arguments for each overlay are parsed, using a parser that
    is only configured to identify the overlay type.

 5. The display arguments for each overlay are parsed again, using a parser
    that is configured to handle arguments specific to the overlay type.


-------------------------------
Adding new command line options
-------------------------------


Most classes in *FSLeyes* derive from the :class:`.HasProperties` class of the
:mod:`props` package. Therefore, with only a couple of excpetions, the
processing of nearly all *FSLeyes* command line arguments is completely
automatic.

Therefore, adding a new command line option is fairly easy.  For example,
let's say you have added a new property on the :class:`.ModelOpts` class,
called ``rotation``::

    class ModelOpts(fsldisplay.DisplayOpts):
        # .
        # .
        # .
        rotation = props.Int(minval=0, maxval=360, clamped=True)
        # .
        # .
        # .

To make this new propery settable via the command line, you need to:

  1. Add an entry to the :data:`OPTIONS` dictionary::

         OPTIONS = td.TypeDict({
             # .
             # .
             # .
             'ModelOpts'      : ['colour',
                                 'outline',
                                 'outlineWidth',
                                 'refImage',
                                 'rotation'],
             # .
             # .
             # .
         })

  2. Specify the command line flags to use, in the :data:`ARGUMENTS`
     dictionary::

         ARGUMENTS = td.TypeDict({
             # .
             # .
             # .
             'ModelOpts.rotation' : ('mr', 'modelRotation'),
             # .
             # .
             # .
         })

  3. Add a description in the :data:`HELP` dictionary::

         HELP = td.TypeDict({
             # .
             # .
             # .
             'ModelOpts.rotation' : 'Rotate the model by this much',
             # .
             # .
             # .
         })


  4. If the property specifies a file/path name (e.g.
     :attr:`.VolumeOpts.clipImage`), add an entry in the :attr:`FILE_OPTIONS`
     dictionary::

         FILE_OPTIONS = td.TypeDict({
             # .
             # .
             # .
             'ModelOpts' : ['refImage', 'rotation'],
             # .
             # .
             # .
         })
"""


from __future__ import print_function

import os.path as op
import            sys
import            logging
import            textwrap
import            argparse
import            functools
import            collections

import props

import fsl.utils.typedict                 as td
import fsl.utils.async                    as async
import fsl.utils.status                   as status
from   fsl.utils.platform import platform as fslplatform
from   .                  import overlay  as fsloverlay


# The colour maps module needs to be imported
# before the displaycontext.opts modules are
# imported, as some of their class definitions
# rely on the colourmaps being initialised
from . import colourmaps 
colourmaps.init()


import displaycontext as fsldisplay
import                   autodisplay


log = logging.getLogger(__name__)


def _get_option_tuples(self, option_string):
    """By default, the ``argparse`` module uses a *prefix matching* strategy,
    which allows the user to (unambiguously) specify only part of an argument.
    
    While this may be a good idea for simple programs with a small number of
    arguments, it is very disruptive to the way that I have designed this
    module.

    To disable this prefix matching functionality, this function is
    monkey-patched into all ArgumentParser instances created in this module.

    
    .. note:: This is unnecessary in python 3.5 and above, due to the addition
              of tge ``allow_abbrev`` option.


    See http://stackoverflow.com/questions/33900846/\
    disable-unique-prefix-matches-for-argparse-and-optparse
    """
    result = []

    # option strings starting with two prefix characters are only
    # split at the '='
    chars = self.prefix_chars
    if option_string[0] in chars and option_string[1] in chars:
        if '=' in option_string:
            option_prefix, explicit_arg = option_string.split('=', 1)
        else:
            option_prefix = option_string
            explicit_arg = None
        for option_string in self._option_string_actions:
            if option_string == option_prefix:
                action = self._option_string_actions[option_string]
                tup = action, option_string, explicit_arg
                result.append(tup)

    # single character options can be concatenated with their arguments
    # but multiple character options always have to have their argument
    # separate
    elif option_string[0] in chars and option_string[1] not in chars:
        option_prefix = option_string
        explicit_arg = None
        short_option_prefix = option_string[:2]
        short_explicit_arg = option_string[2:]

        for option_string in self._option_string_actions:
            if option_string == short_option_prefix:
                action = self._option_string_actions[option_string]
                tup = action, option_string, short_explicit_arg
                result.append(tup)
            elif option_string == option_prefix:
                action = self._option_string_actions[option_string]
                tup = action, option_string, explicit_arg
                result.append(tup)

    # shouldn't ever get here
    else:
        self.error(('unexpected option string: %s') % option_string)

    # return the collected option tuples
    return result


class ArgumentError(Exception):
    """Custom ``Exception`` class raised by ``ArgumentParser`` instances
    created and used in this module.
    """
    pass


def ArgumentParser(*args, **kwargs):
    """Wrapper around the ``argparse.ArgumentParser` constructor which
    creates, monkey-patches, and returns an ``ArgumentParser`` instance.
    """
    import types
    ap = argparse.ArgumentParser(*args, **kwargs)

    def ovlArgError(message):
        raise ArgumentError(message)

    # 1. I don't want prefix matching.
    # 
    # 2. I want to handle argument errors,
    #    rather than having the parser 
    #    force the program to exit
    ap._get_option_tuples = types.MethodType(_get_option_tuples, ap) 
    ap.error              = ovlArgError 
    
    return ap


def concat(lists):
    """Concatenates a list of lists.

    This function is used a few times, and writing concat(lists) is
    nicer-looking than writing lambda blah blah each time.
    """
    return list(functools.reduce(lambda a, b: a + b, lists))


# Names of all of the property which are 
# customisable via command line arguments.
OPTIONS = td.TypeDict({

    'Main'          : ['help',
                       'fullhelp',
                       'verbose',
                       'version',
                       'skipfslcheck',
                       'noisy',
                       'memory',
                       'glversion',
                       'scene',
                       'voxelLoc',
                       'worldLoc',
                       'autoDisplay',
                       'displaySpace'],

    # From here on, all of the keys are
    # the names of HasProperties classes,
    # and all of the values are the 
    # names of properties on them.
    
    'SceneOpts'     : ['showCursor',
                       'bgColour',
                       'cursorColour',
                       'showColourBar',
                       'colourBarLocation',
                       'colourBarLabelSide',
                       'performance'],
    'OrthoOpts'     : ['xzoom',
                       'yzoom',
                       'zzoom',
                       'showLabels',
                       'layout',
                       'showXCanvas',
                       'showYCanvas',
                       'showZCanvas'],
    'LightBoxOpts'  : ['zax',
                       'sliceSpacing',
                       'zrange',
                       'ncols',
                       'nrows',
                       'showGridLines',
                       'highlightSlice'],

    # The order in which properties are listed
    # here is the order in which they are applied.
    'Display'        : ['name',
                        'enabled',
                        'overlayType',
                        'alpha',
                        'brightness',
                        'contrast'],
    'Nifti1Opts'     : ['resolution',
                        'volume'],
    'VolumeOpts'     : ['linkLowRanges',
                        'linkHighRanges',
                        'displayRange',
                        'clippingRange',
                        'invertClipping',
                        'clipImage',
                        'cmap',
                        'negativeCmap',
                        'useNegativeCmap',
                        'interpolation',
                        'invert'],
    'MaskOpts'       : ['colour',
                        'invert',
                        'threshold'],
    'VectorOpts'     : ['xColour',
                        'yColour',
                        'zColour',
                        'suppressX',
                        'suppressY',
                        'suppressZ',
                        'cmap',
                        'colourImage',
                        'modulateImage',
                        'clipImage',
                        'clippingRange'],
    'LineVectorOpts' : ['lineWidth',
                        'directed'],
    'RGBVectorOpts'  : ['interpolation'],
    'ModelOpts'      : ['colour',
                        'outline',
                        'outlineWidth',
                        'refImage',
                        'coordSpace'],
    'TensorOpts'     : ['lighting',
                        'tensorResolution',
                        'tensorScale'],
    'LabelOpts'      : ['lut',
                        'outline',
                        'outlineWidth'],
})
"""This dictionary defines all of the options which are exposed on the command
line.

With the exception of ``Main``, every key is the name of a
:class:`.HasProperties` class, and the list of values are the names of
properties on that class.
"""


# Headings for each of the option groups
GROUPNAMES = td.TypeDict({
    'Main'           : 'Main options',
    'SceneOpts'      : 'Scene options',
    'OrthoOpts'      : 'Ortho display options',
    'LightBoxOpts'   : 'LightBox display options',
    'Display'        : 'Overlay display options',
    'VolumeOpts'     : 'Volume options',
    'MaskOpts'       : 'Mask options',
    'LineVectorOpts' : 'Line vector options',
    'RGBVectorOpts'  : 'RGB vector options',
    'ModelOpts'      : 'Model options',
    'LabelOpts'      : 'Label options',
    'TensorOpts'     : 'Tensor options',
})
"""Command line arguments are grouped according to the class to which
they are applied (see the :data:`ARGUMENTS` dictionary). This dictionary
defines descriptions for ecah command line group.
"""


# Descriptions for each group
GROUPDESCS = td.TypeDict({

    'SceneOpts'    : 'These settings are applied to every '
                     'orthographic and lightbox view.',

    'OrthoOpts'    : 'These settings are applied to every '
                     'ortho view.', 

    'LightBoxOpts' : 'These settings are applied to every '
                     'lightbox view.',
 
    'Display'      : 'Each display option will be applied to the '
                     'overlay which is listed before that option. '
                     'Passing any display option for an overlay will '
                     'override the \'--autoDisplay\' setting for that '
                     'overlay.',

    'VolumeOpts'     : 'These options are applied to \'volume\' overlays.',
    'MaskOpts'       : 'These options are applied to \'mask\' overlays.',
    'LabelOpts'      : 'These options are applied to \'label\' overlays.',
    'LineVectorOpts' : 'These options are applied to \'linevector\' overlays.',
    'RGBVectorOpts'  : 'These options are applied to \'rgbvector\' overlays.',
    'ModelOpts'      : 'These options are applied to \'model\' overlays.',
    'TensorOpts'     : 'These options are applied to \'tensor\' overlays.',
})
"""This dictionary contains descriptions for each argument group. """


# Short/long arguments for all of those options
ARGUMENTS = td.TypeDict({

    'Main.help'            : ('h',  'help'),
    'Main.fullhelp'        : ('fh', 'fullhelp'),
    'Main.verbose'         : ('v',  'verbose'),
    'Main.version'         : ('V',  'version'),
    'Main.skipfslcheck'    : ('S',  'skipfslcheck'),
    'Main.noisy'           : ('n',  'noisy'),
    'Main.memory'          : ('m',  'memory'),
    'Main.glversion'       : ('gl', 'glversion'),
    'Main.scene'           : ('s',  'scene'),
    'Main.voxelLoc'        : ('vl', 'voxelLoc'),
    'Main.worldLoc'        : ('wl', 'worldLoc'),
    'Main.autoDisplay'     : ('ad', 'autoDisplay'),
    'Main.displaySpace'    : ('ds', 'displaySpace'),
    
    'SceneOpts.showColourBar'      : ('cb',  'showColourBar'),
    'SceneOpts.bgColour'           : ('bg',  'bgColour'),
    'SceneOpts.cursorColour'       : ('cc',  'cursorColour'),
    'SceneOpts.colourBarLocation'  : ('cbl', 'colourBarLocation'),
    'SceneOpts.colourBarLabelSide' : ('cbs', 'colourBarLabelSide'),
    'SceneOpts.showCursor'         : ('hc',  'hideCursor'),
    'SceneOpts.performance'        : ('p',   'performance'),
    
    'OrthoOpts.xzoom'       : ('xz', 'xzoom'),
    'OrthoOpts.yzoom'       : ('yz', 'yzoom'),
    'OrthoOpts.zzoom'       : ('zz', 'zzoom'),
    'OrthoOpts.layout'      : ('lo', 'layout'),
    'OrthoOpts.showXCanvas' : ('xh', 'hidex'),
    'OrthoOpts.showYCanvas' : ('yh', 'hidey'),
    'OrthoOpts.showZCanvas' : ('zh', 'hidez'),
    'OrthoOpts.showLabels'  : ('lh', 'hideLabels'),

    'OrthoOpts.xcentre'     : ('xc', 'xcentre'),
    'OrthoOpts.ycentre'     : ('yc', 'ycentre'),
    'OrthoOpts.zcentre'     : ('zc', 'zcentre'),

    'LightBoxOpts.sliceSpacing'   : ('ss', 'sliceSpacing'),
    'LightBoxOpts.ncols'          : ('nc', 'ncols'),
    'LightBoxOpts.nrows'          : ('nr', 'nrows'),
    'LightBoxOpts.zrange'         : ('zr', 'zrange'),
    'LightBoxOpts.showGridLines'  : ('sg', 'showGridLines'),
    'LightBoxOpts.highlightSlice' : ('hs', 'highlightSlice'),
    'LightBoxOpts.zax'            : ('zx', 'zaxis'),

    'Display.name'          : ('n',  'name'),
    'Display.enabled'       : ('d',  'disabled'),
    'Display.overlayType'   : ('ot', 'overlayType'),
    'Display.alpha'         : ('a',  'alpha'),
    'Display.brightness'    : ('b',  'brightness'),
    'Display.contrast'      : ('c',  'contrast'),

    'Nifti1Opts.resolution'   : ('r',  'resolution'),
    'Nifti1Opts.volume'       : ('v',  'volume'),

    'VolumeOpts.displayRange'    : ('dr', 'displayRange'),
    'VolumeOpts.clippingRange'   : ('cr', 'clippingRange'),
    'VolumeOpts.invertClipping'  : ('ic', 'invertClipping'),
    'VolumeOpts.clipImage'       : ('cl', 'clipImage'),
    'VolumeOpts.cmap'            : ('cm', 'cmap'),
    'VolumeOpts.negativeCmap'    : ('nc', 'negativeCmap'),
    'VolumeOpts.useNegativeCmap' : ('un', 'useNegativeCmap'),
    'VolumeOpts.interpolation'   : ('in', 'interpolation'),
    'VolumeOpts.invert'          : ('i',  'invert'),
    'VolumeOpts.linkLowRanges'   : ('ll', 'unlinkLowRanges'),
    'VolumeOpts.linkHighRanges'  : ('lh', 'linkHighRanges'),

    'MaskOpts.colour'    : ('mc', 'maskColour'),
    'MaskOpts.invert'    : ('i',  'maskInvert'),
    'MaskOpts.threshold' : ('t',  'threshold'),

    'VectorOpts.xColour'       : ('xc', 'xColour'),
    'VectorOpts.yColour'       : ('yc', 'yColour'),
    'VectorOpts.zColour'       : ('zc', 'zColour'),
    'VectorOpts.suppressX'     : ('xs', 'suppressX'),
    'VectorOpts.suppressY'     : ('ys', 'suppressY'),
    'VectorOpts.suppressZ'     : ('zs', 'suppressZ'),
    'VectorOpts.cmap'          : ('cm', 'cmap'),
    'VectorOpts.colourImage'   : ('co', 'colourImage'),
    'VectorOpts.modulateImage' : ('mo', 'modulateImage'),
    'VectorOpts.clipImage'     : ('cl', 'clipImage'),
    'VectorOpts.clippingRange' : ('cr', 'clippingRange'),

    'LineVectorOpts.lineWidth'    : ('lw', 'lineWidth'),
    'LineVectorOpts.directed'     : ('ld', 'directed'),
    
    'RGBVectorOpts.interpolation' : ('i',  'interpolation'),

    'TensorOpts.lighting'         : ('dl', 'disableLighting'),
    'TensorOpts.tensorResolution' : ('tr', 'tensorResolution'),
    'TensorOpts.tensorScale'      : ('s',  'scale'),

    'ModelOpts.colour'       : ('mc', 'colour'),
    'ModelOpts.outline'      : ('o',  'outline'),
    'ModelOpts.outlineWidth' : ('w',  'outlineWidth'),
    'ModelOpts.refImage'     : ('r',  'refImage'),
    'ModelOpts.coordSpace'   : ('s',  'coordSpace'),

    'LabelOpts.lut'          : ('l',  'lut'),
    'LabelOpts.outline'      : ('o',  'outline'),
    'LabelOpts.outlineWidth' : ('w',  'outlineWidth'),
})
"""This dictionary defines the short and long command line flags to be used
for every option.

.. note:: 1. There cannot be any collisions between the main options, the 
             :class:`.SceneOpts` options, the :class:`.OrthOpts` options,
             and the :class:`.LightBoxOpts` options.

          2. There cannot be any collisions between the :class:`.Display`
             options and any one set of :class:`.DisplayOpts` options.

          3. There *can* be collisions between these two groups, and 
             between the options for different :class:`.DisplayOpts` types.
"""


# Help text for all of the options
HELP = td.TypeDict({

    'Main.help'          : 'Display basic FSLeyes options and exit',
    'Main.fullhelp'      : 'Display all FSLeyes options and exit',
    'Main.verbose'       : 'Verbose output (can be used up to 3 times)',
    'Main.version'       : 'Print the current fslpy version and exit',
    'Main.skipfslcheck'  : 'Skip $FSLDIR check/warning',
    'Main.noisy'         : 'Make the specified module noisy',
    'Main.memory'        : 'Output memory events (implied if -v is set)',
    'Main.glversion'     : 'Desired (major, minor) OpenGL version',
    'Main.scene'         : 'Scene to show',

    'Main.voxelLoc'        : 'Location to show (voxel coordinates of '
                             'first overlay)',
    'Main.worldLoc'        : 'Location to show (world coordinates of '
                             'first overlay, takes precedence over '
                             '--voxelLoc)', 
    'Main.autoDisplay'     : 'Automatically configure overlay display '
                             'settings (unless any display settings are '
                             'specified)',
    'Main.displaySpace'    : 'Space in which all overlays are displayed - '
                             'can be "pixdim", "world", or a NIFTI image.', 

    'SceneOpts.showCursor'         : 'Do not display the green cursor '
                                     'highlighting the current location',
    'SceneOpts.bgColour'           : 'Canvas background colour',
    'SceneOpts.cursorColour'       : 'Cursor location colour',
    'SceneOpts.showColourBar'      : 'Show colour bar',
    'SceneOpts.colourBarLocation'  : 'Colour bar location',
    'SceneOpts.colourBarLabelSide' : 'Colour bar label orientation',
    'SceneOpts.performance'        : 'Rendering performance '
                                     '(1=fastest, 4=best looking)',
    
    'OrthoOpts.xzoom'       : 'X canvas zoom',
    'OrthoOpts.yzoom'       : 'Y canvas zoom',
    'OrthoOpts.zzoom'       : 'Z canvas zoom',
    'OrthoOpts.layout'      : 'Canvas layout',
    'OrthoOpts.showXCanvas' : 'Hide the X canvas',
    'OrthoOpts.showYCanvas' : 'Hide the Y canvas',
    'OrthoOpts.showZCanvas' : 'Hide the Z canvas',
    'OrthoOpts.showLabels'  : 'Hide orientation labels',

    'OrthoOpts.xcentre'     : 'X canvas display centre (YZ world coordinates '
                              'of first overlay)',
    'OrthoOpts.ycentre'     : 'Y canvas display centre (XZ world coordinates '
                              'of first overlay)', 
    'OrthoOpts.zcentre'     : 'Z canvas display centre (XY world coordinates '
                              'of first overlay)', 

    'LightBoxOpts.sliceSpacing'   : 'Slice spacing',
    'LightBoxOpts.ncols'          : 'Number of columns',
    'LightBoxOpts.nrows'          : 'Number of rows',
    'LightBoxOpts.zrange'         : 'Slice range',
    'LightBoxOpts.showGridLines'  : 'Show grid lines',
    'LightBoxOpts.highlightSlice' : 'Highlight current slice',
    'LightBoxOpts.zax'            : 'Z axis',

    'Display.name'          : 'Overlay name',
    'Display.enabled'       : 'Disable (hide) overlay',
    'Display.overlayType'   : 'Overlay type',
    'Display.alpha'         : 'Opacity',
    'Display.brightness'    : 'Brightness',
    'Display.contrast'      : 'Contrast',

    'Nifti1Opts.resolution' : 'Resolution',
    'Nifti1Opts.volume'     : 'Volume',

    'VolumeOpts.displayRange'    : 'Display range. Setting this will '
                                   'override brightnes/contrast settings.',
    'VolumeOpts.clippingRange'   : 'Clipping range. Setting this will '
                                   'override the low display range (unless '
                                   'low ranges are unlinked).', 
    'VolumeOpts.invertClipping'  : 'Invert clipping',
    'VolumeOpts.clipImage'       : 'Image containing clipping values '
                                   '(defaults to the image itself)' ,
    'VolumeOpts.cmap'            : 'Colour map',
    'VolumeOpts.negativeCmap'    : 'Colour map for negative values '
                                   '(only used if the negative '
                                   'colour map is enabled)', 
    'VolumeOpts.useNegativeCmap' : 'Use negative colour map',
    'VolumeOpts.interpolation'   : 'Interpolation',
    'VolumeOpts.invert'          : 'Invert colour map',
    'VolumeOpts.linkLowRanges'   : 'Unlink low display/clipping ranges',
    'VolumeOpts.linkHighRanges'  : 'Link high display/clipping ranges',

    'MaskOpts.colour'    : 'Colour',
    'MaskOpts.invert'    : 'Invert',
    'MaskOpts.threshold' : 'Threshold',

    'VectorOpts.xColour'       : 'X colour',
    'VectorOpts.yColour'       : 'Y colour',
    'VectorOpts.zColour'       : 'Z colour',
    'VectorOpts.suppressX'     : 'Suppress X magnitude',
    'VectorOpts.suppressY'     : 'Suppress Y magnitude',
    'VectorOpts.suppressZ'     : 'Suppress Z magnitude',
    'VectorOpts.cmap'          : 'Colour map (only used if a '
                                 'colour image is provided)',
    'VectorOpts.colourImage'   : 'Image to colour vectors with',
    'VectorOpts.modulateImage' : 'Image to modulate vector brightness with',
    'VectorOpts.clipImage'     : 'Image to clip vectors with',
    'VectorOpts.clippingRange' : 'Clipping range (only used if a '
                                 'clipping image is provided)', 

    'LineVectorOpts.lineWidth'    : 'Line width',
    'LineVectorOpts.directed'     : 'Interpret vectors as directed',
    'RGBVectorOpts.interpolation' : 'Interpolation',

    'ModelOpts.colour'       : 'Model colour',
    'ModelOpts.outline'      : 'Show model outline',
    'ModelOpts.outlineWidth' : 'Model outline width',
    'ModelOpts.refImage'     : 'Reference image for model',

    'TensorOpts.lighting'         : 'Disable lighting effect',
    'TensorOpts.tensorResolution' : 'Tensor resolution (quality)',
    'TensorOpts.tensorScale'      : 'Tensor size (percentage of voxel size)',
    
    'LabelOpts.lut'          : 'Label image LUT',
    'LabelOpts.outline'      : 'Show label outlines',
    'LabelOpts.outlineWidth' : 'Label outline width', 
})
"""This dictionary defines the help text for all command line options."""


# Extra settings for some properties, passed through 
# to the props.cli.addParserArguments function.
EXTRA = td.TypeDict({
    'Display.overlayType' : {'choices' : fsldisplay.ALL_OVERLAY_TYPES,
                             'default' : fsldisplay.ALL_OVERLAY_TYPES[0]},

    'LabelOpts.lut'       : {
        # The LabelOpts.lut choice property has
        # LookupTable instances as values, which
        # obviously cannot be passed in on the
        # command line. But the lut property will
        # also accept the lut key as an alternate
        # value, so we accept these on the command
        # line instead. See the colourmaps and
        # labelopts modules for more detail.
        'choices'       : [],
        'useAlts'       : True
    }
})
"""This dictionary defines any extra settings to be passed through
to the :func:`.props.addParserArguments` function.
"""


# File options need special treatment
FILE_OPTIONS = td.TypeDict({
    'Main'       : ['displaySpace'],
    'VolumeOpts' : ['clipImage'],
    'VectorOpts' : ['clipImage',
                    'colourImage',
                    'modulateImage'],
    'ModelOpts'  : ['refImage'],
})
"""Arguments which accept file or path names need special treatment.
This is because the first step in the :func:`parseArgs` function is
to search through the list of arguments, and identify all arguments
which look like overlays. This procedure needs to figure out whether
an argument that looks like an overlay (i.e. a file/directory path)
is actually an overlay, or is an argument for another overlay.
""" 


# Transform functions for properties where the
# value passed in on the command line needs to
# be manipulated before the property value is
# set
#
# TODO If/when you have a need for more
# complicated property transformations (i.e.
# non-reversible ones), you'll need to have
# an inverse transforms dictionary
def _imageTrans(i):
    
    stri = str(i).lower()
    
    if   i    is  None:    return None
    elif stri == 'none':   return None

    # Special cases for Main.displaySpace
    elif stri == 'world':  return 'world'
    elif stri == 'pixdim': return 'pixdim'

    else:                  return i.dataSource


def _lutTrans(l):
    if isinstance(l, colourmaps.LookupTable): return l.key
    else:                                     return l

    
TRANSFORMS = td.TypeDict({
    'SceneOpts.showCursor'     : lambda b: not b,
    'OrthoOpts.showXCanvas'    : lambda b: not b,
    'OrthoOpts.showYCanvas'    : lambda b: not b,
    'OrthoOpts.showZCanvas'    : lambda b: not b,
    'OrthoOpts.showLabels'     : lambda b: not b,
    'Display.enabled'          : lambda b: not b,
    'VolumeOpts.linkLowRanges' : lambda b: not b,
    'TensorOpts.lighting'      : lambda b: not b, 
    'LabelOpts.lut'            : _lutTrans,
})
"""This dictionary defines any transformations for command line options
where the value passed on the command line cannot be directly converted
into the corresponding property value.
"""

# All of the file options need special treatment
for target, fileOpts in FILE_OPTIONS.items():
    for fileOpt in fileOpts:
        key             = '{}.{}'.format(target, fileOpt)
        TRANSFORMS[key] = _imageTrans


def _setupMainParser(mainParser):
    """Sets up an argument parser which handles options related
    to the scene. This function configures the following argument
    groups:
    
      - *Main*:         Top level optoins
      - *SceneOpts*:    Common scene options
      - *OrthoOpts*:    Options related to setting up a orthographic display
      - *LightBoxOpts*: Options related to setting up a lightbox display
    """

    # FSLEyes application options

    # Options defining the overall scene,
    # and separate parser groups for scene
    # settings, ortho, and lightbox.
 
    mainGroup  = mainParser.add_argument_group(GROUPNAMES[    'Main'],
                                               GROUPDESCS.get('Main'))
    sceneGroup = mainParser.add_argument_group(GROUPNAMES[    'SceneOpts'],
                                               GROUPDESCS.get('SceneOpts'))
    orthoGroup = mainParser.add_argument_group(GROUPNAMES[    'OrthoOpts'],
                                               GROUPDESCS.get('OrthoOpts'))
    lbGroup    = mainParser.add_argument_group(GROUPNAMES[    'LightBoxOpts'],
                                               GROUPDESCS.get('LightBoxOpts'))

    _configMainParser(     mainGroup)
    _configSceneParser(    sceneGroup)
    _configOrthoParser(    orthoGroup)
    _configLightBoxParser( lbGroup)


def _configParser(target, parser, propNames=None):
    """Configures the given parser so it will parse arguments for the
    given target.
    """

    if propNames is None:
        propNames = OPTIONS[target]
    shortArgs = {}
    longArgs  = {}
    helpTexts = {}
    extra     = {}

    for propName in propNames:

        shortArg, longArg = ARGUMENTS[ target, propName]
        helpText          = HELP .get((target, propName), 'nohelp')
        propExtra         = EXTRA.get((target, propName), None)

        shortArgs[propName] = shortArg
        longArgs[ propName] = longArg
        helpTexts[propName] = helpText

        if propExtra is not None:
            extra[propName] = propExtra

    props.addParserArguments(target,
                             parser,
                             cliProps=propNames,
                             shortArgs=shortArgs,
                             longArgs=longArgs,
                             propHelp=helpTexts,
                             extra=extra)


def _configMainParser(mainParser):
    """Adds options to the given parser which allow the user to specify
    *main* FSLeyes options.
    """
    mainArgs = {name: ARGUMENTS['Main', name] for name in OPTIONS['Main']}
    mainHelp = {name: HELP[     'Main', name] for name in OPTIONS['Main']}

    for name, (shortArg, longArg) in mainArgs.items():
        mainArgs[name] = ('-{}'.format(shortArg), '--{}'.format(longArg))

    mainParser.add_argument(*mainArgs['help'],
                            action='store_true',
                            help=mainHelp['help'])
    mainParser.add_argument(*mainArgs['fullhelp'],
                            action='store_true',
                            help=mainHelp['fullhelp'])
    mainParser.add_argument(*mainArgs['version'],
                            action='store_true',
                            help=mainHelp['version'])
    mainParser.add_argument(*mainArgs['skipfslcheck'],
                            action='store_true',
                            help=mainHelp['skipfslcheck'])

    # Debug messages are stripped from frozen
    # versions of FSLeyes, so there's no point
    # in keeping these arguments.
    if not fslplatform.frozen:
        mainParser.add_argument(*mainArgs['verbose'],
                                action='count',
                                help=mainHelp['verbose'])
        mainParser.add_argument(*mainArgs['noisy'],
                                metavar='MODULE',
                                action='append',
                                help=mainHelp['noisy'])
        mainParser.add_argument(*mainArgs['memory'],
                                action='store_true',
                                help=mainHelp['memory'])
        
    mainParser.add_argument(*mainArgs['glversion'],
                            metavar=('MAJOR', 'MINOR'),
                            type=int,
                            nargs=2,
                            help=mainHelp['glversion'])
    mainParser.add_argument(*mainArgs['scene'],
                            help=mainHelp['scene'])
    mainParser.add_argument(*mainArgs['voxelLoc'],
                            metavar=('X', 'Y', 'Z'),
                            type=int,
                            nargs=3,
                            help=mainHelp['voxelLoc'])
    mainParser.add_argument(*mainArgs['worldLoc'],
                            metavar=('X', 'Y', 'Z'),
                            type=float,
                            nargs=3,
                            help=mainHelp['worldLoc'])
    mainParser.add_argument(*mainArgs['autoDisplay'],
                            action='store_true',
                            help=mainHelp['autoDisplay'])
    mainParser.add_argument(*mainArgs['displaySpace'],
                            type=str,
                            help=mainHelp['displaySpace'])
    

def _configSceneParser(sceneParser):
    """Adds options to the given argument parser which allow
    the user to specify :class:`.SceneOpts` properties.
    """
    _configParser(fsldisplay.SceneOpts, sceneParser)
   

def _configOrthoParser(orthoParser):
    """Adds options to the given parser allowing the user to
    configure :class:`.OrthoOpts` properties.
    """

    OrthoOpts = fsldisplay.OrthoOpts
    _configParser(OrthoOpts, orthoParser)
                             
    # Extra configuration options that are
    # not OrthoPanel properties, so can't
    # be automatically set up
    for opt, metavar in zip(['xcentre',  'ycentre',  'zcentre'],
                            [('Y', 'Z'), ('X', 'Z'), ('X', 'Y')]):
        
        shortArg, longArg = ARGUMENTS[OrthoOpts, opt]
        helpText          = HELP[     OrthoOpts, opt]

        shortArg =  '-{}'.format(shortArg)
        longArg  = '--{}'.format(longArg)

        orthoParser.add_argument(shortArg,
                                 longArg,
                                 metavar=metavar,
                                 type=float,
                                 nargs=2,
                                 help=helpText)


def _configLightBoxParser(lbParser):
    """Adds options to the given parser allowing the user to
    configure :class:`.LightBoxOpts` properties.
    """    
    _configParser(fsldisplay.LightBoxOpts, lbParser)


def _setupOverlayParsers(forHelp=False):
    """Creates a set of parsers which handle command line options for
    :class:`.Display` instances, and for all :class:`.DisplayOpts` instances.

    :arg forHelp: If ``False`` (the default), each of the parsers created
                  to handle options for the :class:`.DisplayOpts`
                  sub-classes will be configured so that the can also
                  handle options for :class:`.Display` properties. Otherwise,
                  the ``DisplayOpts`` parsers will be configured to only
                  handle ``DisplayOpts`` properties. This option is available
                  to make it easier to separate the help sections when
                  printing help.

    :returns: A tuple containing:
    
                - An ``ArgumentParser`` which parses arguments specifying
                  the :class:`.Display` properties. This parser is not 
                  actually used to parse arguments - it is only used to
                  generate help text.
    
                - An ``ArgumentParser`` which just parses arguments specifying
                  the :attr:`.Display.overlayType` property. 

                - An ``ArgumentParser`` which parses arguments specifying
                  :class:`.Display` and :class:`.DisplayOpts` properties.
    """

    Display        = fsldisplay.Display
    VolumeOpts     = fsldisplay.VolumeOpts
    RGBVectorOpts  = fsldisplay.RGBVectorOpts
    LineVectorOpts = fsldisplay.LineVectorOpts
    TensorOpts     = fsldisplay.TensorOpts
    MaskOpts       = fsldisplay.MaskOpts
    ModelOpts      = fsldisplay.ModelOpts
    LabelOpts      = fsldisplay.LabelOpts

    # A parser is created and returned
    # for each one of these types.
    parserTypes = [VolumeOpts, MaskOpts, LabelOpts,
                   ModelOpts, LineVectorOpts,
                   RGBVectorOpts, TensorOpts]

    # Dictionary containing the Display parser,
    # and parsers for each overlay type. We use
    # an ordered dict to control the order in
    # which help options are printed.
    parsers = collections.OrderedDict()

    # The Display parser is used as a parent
    # for each of the DisplayOpts parsers
    otParser   = ArgumentParser(add_help=False)
    dispParser = ArgumentParser(add_help=False)
    dispProps  = list(OPTIONS[Display])

    if not forHelp:
        dispProps.remove('overlayType')
    
    _configParser(Display, dispParser, dispProps)
    _configParser(Display, otParser,   ['overlayType'])

    # Create and configure
    # each of the parsers
    for target in parserTypes:

        if not forHelp: parents = [dispParser]
        else:           parents = []

        parser = ArgumentParser(prog='', add_help=False, parents=parents)
        
        parsers[target] = parser
        propNames       = concat(OPTIONS.get(target, allhits=True))
        specialOptions  = []
        
        # The file options need
        # to be configured manually.
        fileOpts = FILE_OPTIONS.get(target, [])
        for propName in fileOpts:
            if propName in propNames:
                specialOptions.append(propName)
                propNames     .remove(propName)

        _configParser(target, parser, propNames)

        # We need to process the special options
        # manually, rather than using the props.cli
        # module - see the handleOverlayArgs function.
        for opt in specialOptions:
            shortArg, longArg = ARGUMENTS[target, opt]
            helpText          = HELP.get((target, opt), 'no help')

            shortArg =  '-{}'.format(shortArg)
            longArg  = '--{}'.format(longArg)
            parser.add_argument(
                shortArg,
                longArg,
                metavar='FILE',
                help=helpText)

    return dispParser, otParser, parsers

            
def parseArgs(mainParser,
              argv,
              name,
              desc,
              toolOptsDesc='[options]',
              fileOpts=None):
    """Parses the given command line arguments, returning an
    :class:`argparse.Namespace` object containing all the arguments.

    The display options for individual overlays are parsed separately. The
    :class:`~argparse.Namespace` objects for each overlay are returned in a
    list, stored as an attribute, called ``overlays``, of the returned
    top-level ``Namespace`` instance. Each of the overlay ``Namespace``
    instances also has an attribute, called ``overlay``, which contains the
    full path of the overlay file that was speciied.

      - mainParser:   A :class:`argparse.ArgumentParser` which should be
                      used as the top level parser.
    
      - argv:         The arguments as passed in on the command line.
    
      - name:         The name of the tool - this function might be called by
                      either the :mod:`~fsl.tools.fsleyes` tool or the
                      :mod:`~fsl.tools.render` tool.
    
      - desc:         A description of the tool.
    
      - toolOptsDesc: A string describing the tool-specific options (those
                      options which are handled by the tool, not by this
                      module).


      - fileOpts:     If the ``mainParser`` has already been configured to
                      accept some arguments, you must pass any arguments
                      that accept a file name as a list here. Otherwise,
                      the file name may be incorrectly identified as a
                      path to an overlay.
    """

    if fileOpts is None: fileOpts = []
    else:                fileOpts = list(fileOpts)

    log.debug('Parsing arguments for {}: {}'.format(name, argv))

    # I hate argparse. By default, it does not support
    # the command line interface that I want to provide,
    # as demonstrated in this usage string. 
    usageStr   = '{} {} [overlayfile [displayOpts]] '\
                 '[overlayfile [displayOpts]] ...'.format(
                     name,
                     toolOptsDesc)

    # So I'm using multiple argument parsers. First
    # of all, the mainParser parses application
    # options. We'll create additional parsers for
    # handling overlays a bit later on.
    mainParser.usage       = usageStr
    mainParser.prog        = name
    mainParser.description = desc

    _setupMainParser(mainParser)

    # Figure out where the overlay files
    # are in the argument list, accounting
    # for any options which accept file
    # names as arguments.
    # 
    # Make a list of all the options which
    # accept filenames, and which we need
    # to account for when we're searching
    # for overlay files, flattening the
    # short/long arguments into a 1D list.
    for target, propNames in FILE_OPTIONS.items():
        for propName in propNames:
            fileOpts.extend(ARGUMENTS[target, propName])

    # There is a possibility that the user
    # may specify an overlay name which is the
    # same as the overlay file - so we make
    # sure that such situations don't result
    # in an overlay file match.
    fileOpts.extend(ARGUMENTS[fsldisplay.Display, 'name'])

    log.debug('Identifying overlay paths (ignoring: {})'.format(fileOpts))

    # Compile a list of arguments which
    # look like overlay file names
    ovlIdxs  = []
    ovlTypes = []
    
    for i in range(len(argv)):

        # See if the current argument looks like a data source
        dtype, fname = fsloverlay.guessDataSourceType(argv[i])

        # If the file name refers to a file that
        # does not exist, assume it is an argument
        if not op.exists(fname):
            continue

        # Check that this overlay file was 
        # not a parameter to a file option
        if i > 0 and argv[i - 1].strip('-') in fileOpts:
            continue

        # Unrecognised overlay type -
        # I don't know what to do
        if dtype is None:
            raise RuntimeError('Unrecognised overlay type: {}'.format(fname))

        # Otherwise, it's an overlay
        # file that needs to be loaded
        ovlIdxs .append(i)
        ovlTypes.append(dtype)

    # Why is this here?
    ovlIdxs.append(len(argv))

    # Separate the program arguments 
    # from the overlay display arguments
    progArgv = argv[:ovlIdxs[0]]
    ovlArgv  = argv[ ovlIdxs[0]:]

    log.debug('Main arguments:    {}'.format(progArgv))
    log.debug('Overlay arguments: {}'.format(ovlArgv))

    # Parse the application options with the mainParser
    try:
        namespace = mainParser.parse_args(progArgv)
        
    except ArgumentError as e:
        print(e.message)
        print() 
        mainParser.print_usage()
        sys.exit(1)

    if namespace.help:
        _printShortHelp(mainParser)
        sys.exit(0)

    if namespace.fullhelp:
        _printFullHelp(mainParser)
        sys.exit(0)

    # Now, we'll create additiona parsers to handle
    # the Display and DisplayOpts options for each
    # overlay . Below we're going to manually step
    # through the list of arguments for each overlay,
    # and perform a two-pass parse on them.
    #
    # First, we use a parser which is only configured
    # to handle arguments for the Display.overlayType
    # property.
    #
    # Then, now that we know the overlay type, we can
    # then use the appropriate display opts parser to
    # handle the rest of the options.
    dispParser, otParser, optParsers = _setupOverlayParsers()
 
    # Parse each block of display options one by one,
    # and aggregate the results into a list attached
    # to the main parser namespace object.
    namespace.overlays = []
    
    for i in range(len(ovlIdxs) - 1):

        ovlArgv = argv[ovlIdxs[i]:ovlIdxs[i + 1]]
        ovlFile = ovlArgv[ 0]
        ovlType = ovlTypes[i]
        ovlArgv = ovlArgv[ 1:]

        # First use the overlay type parser 
        # to see if the user has explicitly
        # specified an overlay type
        try:
            otArgs, remaining = otParser.parse_known_args(ovlArgv)
        
        except ArgumentError as e:
            print(e.message)
            print()
            mainParser.print_usage()
            sys.exit(1)

        # If the user did not specify an overlay type
        # for this overlay, use its default (see the
        # display.OVERLAY_TYPES) dictionary)
        if otArgs.overlayType is None:
            otArgs.overlayType = fsldisplay.OVERLAY_TYPES[ovlType][0]

        # Now parse the Display/DisplayOpts
        # with the appropriate parser
        optType   = fsldisplay.DISPLAY_OPTS_MAP[otArgs.overlayType]
        optParser = optParsers[optType]

        try: optArgs = optParser.parse_args(remaining)
        
        except ArgumentError as e:
            print(e.message)
            print()
            mainParser.print_usage()
            print()
            print('Options for \'{}\' overlays'.format(otArgs.overlayType))
            optUsage = optParser.format_usage()
            
            # Remove the 'usage: ' prefix
            # generated by argparse
            optUsage = '      ' + optUsage[6:]
            
            print(optUsage)
            sys.exit(1)
 

        # Attach the path and the overlay
        # type to the opts namespace object
        optArgs.overlayType = otArgs.overlayType
        optArgs.overlay     = ovlFile

        # We just add a list of argparse.Namespace
        # objects, one for each overlay, to
        # the parent Namespace object.
        namespace.overlays.append(optArgs)

    return namespace


def _printShortHelp(mainParser):
    """Prints out help for a selection of arguments.

    :arg mainParser: The top level ``ArgumentParser``.
    """

    # First, we build a list of all arguments
    # that are handled by the main parser.
    # This is done so that we can differentiate
    # between arguments added by this module, and
    # arguments added by users of this module 
    # (e.g. the render tool adds a few arguments
    # to the main parser before it is passed to
    # this module for configuration).

    allMain     = OPTIONS['Main']
    allScene    = OPTIONS['SceneOpts']
    allOrtho    = OPTIONS['OrthoOpts']
    allLightBox = OPTIONS['LightBoxOpts']

    allMainArgs =  \
        [ARGUMENTS['Main.{}'        .format(o)] for o in allMain]  + \
        [ARGUMENTS['SceneOpts.{}'   .format(o)] for o in allScene] + \
        [ARGUMENTS['OrthoOpts.{}'   .format(o)] for o in allOrtho] + \
        [ARGUMENTS['LightBoxOpts.{}'.format(o)] for o in allLightBox]
    allMainArgs = ['--{}'.format(a[1]) for a in allMainArgs]
 
    # Now we build a list of all arguments
    # that we want to show help for, in this
    # shortened help page.
    mainArgs    = ['help', 'fullhelp', 'scene', 'autoDisplay']
    displayArgs = ['overlayType', 'alpha', 'brightness', 'contrast']
    volumeArgs  = ['displayRange', 'clippingRange', 'cmap', 'linkLowRanges']

    mainArgs    = [ARGUMENTS['Main.{}'      .format(a)] for a in mainArgs]
    displayArgs = [ARGUMENTS['Display.{}'   .format(a)] for a in displayArgs]
    volumeArgs  = [ARGUMENTS['VolumeOpts.{}'.format(a)] for a in volumeArgs]

    mainArgs    = ['--{}'.format(a[1]) for a in mainArgs]
    displayArgs = ['--{}'.format(a[1]) for a in displayArgs]
    volumeArgs  = ['--{}'.format(a[1]) for a in volumeArgs]

    allArgs = td.TypeDict({
        'Main'       : mainArgs,
        'Display'    : displayArgs,
        'VolumeOpts' : volumeArgs})

    # The public argparse API is quite inflexible
    # with respect to dynamic modification of
    # arguments and help text. Here I'm using
    # undocumented attributes and features to
    # suppress the help text for argument groups
    # and arguments...

    # Suppress all of the main parser argument groups
    for group in mainParser._action_groups:
        group.title       = argparse.SUPPRESS
        group.description = argparse.SUPPRESS

    # Suppress main parser arguments that we
    # don't want to show
    for action in mainParser._actions:

        # We want to show any arguments that are
        # defined outside of this module (e.g. render)
        if all([o not in allMainArgs for o in action.option_strings]):
            continue

        # We don't want to show any other argument that
        # are not specified in the hard coded mainArgs
        # list above.
        if all([o not in allArgs['Main'] for o in action.option_strings]):
            action.help = argparse.SUPPRESS

    # Generate the help text for main options
    helpText = mainParser.format_help()

    # Now configure Display/DisplayOpts parsers
    dispParser, _, optParsers = _setupOverlayParsers(forHelp=True)
    parsers = ([(fsldisplay.Display,    dispParser),
                (fsldisplay.VolumeOpts, optParsers[fsldisplay.VolumeOpts])])

    for target, parser in parsers:

        args = allArgs[target]

        # Suppress all arguments that
        # are not listed above
        for action in parser._actions:
            if all([o not in args for o in action.option_strings]):
                action.help = argparse.SUPPRESS

        groupName = GROUPNAMES.get(target, None)
        groupDesc = GROUPDESCS.get(target, None)

        groupDesc = '\n  '.join(textwrap.wrap(groupDesc, 60))

        helpText += '\n' + groupName + ':\n'
        if groupDesc is not None:
            helpText += '  ' + groupDesc + '\n'

        ovlHelp = parser.format_help()

        skipTo    = 'optional arguments:'
        optStart  = ovlHelp.index(skipTo)
        optStart += len(skipTo) + 1
        ovlHelp   = ovlHelp[optStart:]

        helpText += '\n' + ovlHelp

    print(helpText)
    

def _printFullHelp(mainParser):
    """Prints out help for all arguments.

    :arg mainParser: The top level ``ArgumentParser``.
    """ 

    # Create a bunch of parsers for handling
    # overlay display options
    dispParser, _, optParsers = _setupOverlayParsers(forHelp=True)

    # Print help for the main parser first,
    # and then separately for the overlay parser
    helpText = mainParser.format_help()

    optParsers = ([(fsldisplay.Display, dispParser)] +
                  list(optParsers.items()))

    for target, parser in optParsers:

        groupName = GROUPNAMES.get(target, None)
        groupDesc = GROUPDESCS.get(target, None)

        groupDesc = '\n  '.join(textwrap.wrap(groupDesc, 60))

        helpText += '\n' + groupName + ':\n'
        if groupDesc is not None:
            helpText += '  ' + groupDesc + '\n'

        ovlHelp = parser.format_help()

        skipTo    = 'optional arguments:'
        optStart  = ovlHelp.index(skipTo)
        optStart += len(skipTo) + 1
        ovlHelp   = ovlHelp[optStart:]

        helpText += '\n' + ovlHelp

    print(helpText) 


def _applyArgs(args, target, propNames=None):
    """Applies the given command line arguments to the given target object."""

    if propNames is None:
        propNames = concat(OPTIONS.get(target, allhits=True))
        
    longArgs  = {name : ARGUMENTS[target, name][1] for name in propNames}
    xforms    = {}
    
    for name in propNames:
        xform = TRANSFORMS.get((target, name), None)
        if xform is not None:
            xforms[name] = xform

    log.debug('Applying arguments to {}: {}'.format(
        type(target).__name__,
        propNames))

    props.applyArguments(target,
                         args,
                         propNames=propNames,
                         xformFuncs=xforms,
                         longArgs=longArgs)


def _generateArgs(source, propNames=None):
    """Does the opposite of :func:`_applyArgs` - generates command line
    arguments which can be used to configure another ``source`` instance
    in the same way as the provided one.
    """

    if propNames is None:
        propNames = concat(OPTIONS.get(source, allhits=True))
        
    longArgs  = {name : ARGUMENTS[source, name][1] for name in propNames}
    xforms    = {}
    
    for name in propNames:
        xform = TRANSFORMS.get((source, name), None)
        if xform is not None:
            xforms[name] = xform

    return props.generateArguments(source,
                                   xformFuncs=xforms,
                                   cliProps=propNames,
                                   longArgs=longArgs)


def calcCanvasCentres(args, overlayList, displayCtx):
    """Convenience function which calculates and returns the locations of the
    ``xcentre``, ``ycentre``, and ``zcentre`` arguments, in the display
    coordinate system.

    .. todo:: Allow transformation in both directions.
    """

    loc = displayCtx.location.xyz
    xc  = args.xcentre
    yc  = args.ycentre
    zc  = args.zcentre

    if len(overlayList) == 0:
        return ((loc[1], loc[2]),
                (loc[0], loc[2]),
                (loc[0], loc[1]))

    opts   = displayCtx.getOpts(overlayList[0])
    refimg = opts.getReferenceImage()

    # This overlay has no referecne image -
    # therefore its world coordinate system
    # is equivalent to the display coordinate
    # system.
    if refimg is None:
        if xc is None: xc = (loc[1], loc[2])
        if yc is None: yc = (loc[0], loc[2])
        if zc is None: zc = (loc[0], loc[1])
        return xc, yc, zc

    # Transform the display location into
    # world coordinates of the overlay
    loc = opts.transformCoords([loc], 'display', 'world')[0]

    if xc is None: xc = (loc[1], loc[2])
    if yc is None: yc = (loc[0], loc[2])
    if zc is None: zc = (loc[0], loc[1])

    # Fill in the horizontal/vertical coordinates
    xc  = [loc[0], xc[ 0], xc[ 1]]
    yc  = [yc[ 0], loc[1], yc[ 1]]
    zc  = [zc[ 0], zc[ 1], loc[2]]

    # Transform them from the overlay world
    # coordinates into display coordinates
    xc, yc, zc = opts.transformCoords([xc, yc, zc], 'world', 'display')

    xc = xc[1], xc[2]
    yc = yc[0], yc[2]
    zc = zc[0], zc[1]

    return xc, yc, zc


def applySceneArgs(args, overlayList, displayCtx, sceneOpts):
    """Configures the scene displayed by the given :class:`.DisplayContext`
    instance according to the arguments that were passed in on the command
    line.

    .. note:: The scene arguments are applied asynchronously using
              :func:`.async.idle`. This is done because the
              :func:`.applyOverlayArgs` function also applies its
              arguments asynchrnously, and we want the order of
              application to match the order in which these functions
              were called.

    :arg args:        :class:`argparse.Namespace` object containing the parsed
                      command line arguments.

    :arg overlayList: A :class:`.OverlayList` instance.

    :arg displayCtx:  A :class:`.DisplayContext` instance.

    :arg sceneOpts:   A :class:`.SceneOpts` instance.
    """

    def apply():

        import fsl.data.image as fslimage

        # First apply all command line options
        # related to the display context...

        # Display space may be a string,
        # or a path to an image
        if args.displaySpace is not None and op.exists(args.displaySpace):
            displaySpace = _findOrLoad(overlayList,
                                       args.displaySpace,
                                       fslimage.Image)
        else:
            displaySpace = args.displaySpace

        if displaySpace is not None:
            displayCtx.displaySpace = displaySpace

        # Set the selected overlay 
        # to the last specified
        if len(overlayList) > 0:
            displayCtx.selectedOverlay = len(overlayList) - 1

        # Auto display
        displayCtx.autoDisplay = args.autoDisplay

        # voxel/world location
        if len(overlayList) > 0:

            defaultLoc = [displayCtx.bounds.xlo + 0.5 * displayCtx.bounds.xlen,
                          displayCtx.bounds.ylo + 0.5 * displayCtx.bounds.ylen,
                          displayCtx.bounds.zlo + 0.5 * displayCtx.bounds.zlen]
            
            opts   = displayCtx.getOpts(overlayList[0])
            refimg = opts.getReferenceImage()
            
            if refimg is None:
                displayLoc = defaultLoc
            else:
                refOpts = displayCtx.getOpts(refimg)

                if args.worldLoc:
                    displayLoc = refOpts.transformCoords([args.worldLoc],
                                                         'world',
                                                         'display')[0]
                    
                elif args.voxelLoc:
                    displayLoc = refOpts.transformCoords([args.voxelLoc],
                                                         'voxel',
                                                         'display')[0]

                else:
                    displayLoc = defaultLoc

            displayCtx.location.xyz = displayLoc

        # Now, apply arguments to the SceneOpts instance
        _applyArgs(args, sceneOpts)
        
    async.idle(apply)


def generateSceneArgs(overlayList, displayCtx, sceneOpts, exclude=None):
    """Generates command line arguments which describe the current state of
    the provided ``displayCtx`` and ``sceneOpts`` instances.

    :arg overlayList: A :class:`.OverlayList` instance.

    :arg displayCtx:  A :class:`.DisplayContext` instance.

    :arg sceneOpts:   A :class:`.SceneOpts` instance.
    
    :arg exclude:     A list of property names to exclude.
    """

    if exclude is None:
        exclude = []

    args = []

    args += ['--{}'.format(ARGUMENTS['Main.scene'][1])]
    if   isinstance(sceneOpts, fsldisplay.OrthoOpts):    args += ['ortho']
    elif isinstance(sceneOpts, fsldisplay.LightBoxOpts): args += ['lightbox']
    else: raise ValueError('Unrecognised SceneOpts '
                           'type: {}'.format(type(sceneOpts).__name__))

    # main options
    if len(overlayList) > 0:

        # Get the world location, in
        # terms of the first overlay 
        worldLoc = displayCtx.location.xyz
        opts     = displayCtx.getOpts(overlayList[0])
        refimg   = opts.getReferenceImage()
        
        if refimg is not None:
            refOpts  = displayCtx.getOpts(refimg)
            worldLoc = refOpts.transformCoords([worldLoc],
                                               'display',
                                               'world')[0] 

        args += ['--{}'.format(ARGUMENTS['Main.worldLoc'][1])]
        args += ['{}'.format(c) for c in worldLoc]

    props = OPTIONS.get(sceneOpts, allhits=True)

    props = [p for p in props if p not in exclude]
    args += _generateArgs(sceneOpts, concat(props))

    return args


def generateOverlayArgs(overlay, displayCtx):
    """Generates command line arguments which describe the display
    of the current overlay.

    :arg overlay:    An overlay object.

    :arg displayCtx: A :class:`.DisplayContext` instance.
    """
    display = displayCtx.getDisplay(overlay)
    opts    = display   .getDisplayOpts()
    args    = _generateArgs(display) + _generateArgs(opts)

    return args


def applyOverlayArgs(args, overlayList, displayCtx, **kwargs):
    """Loads and configures any overlays which were specified on the
    command line.

    .. warning:: This function uses the :func:`.overlay.loadOverlay` function
                 which in turn uses :func:`.async.idle` to load the overlays.
                 This means that the overlays are loaded and configured
                 asynchronously, meaning that they may not be loaded by the
                 time that this function returns. See the
                 :func:`.overlay.loadOverlay` documentation for more details.

    :arg args:        A :class:`~argparse.Namespace` instance, as returned
                      by the :func:`parseArgs` function.
    
    :arg overlayList: An :class:`.OverlayList` instance, to which the
                      overlays should be added.
    
    :arg displayCtx:  A :class:`.DisplayContext` instance, which manages the
                      scene and overlay display.
    
    :arg kwargs:      Passed through to the :func:`.Overlay.loadOverlays`
                      function.
    """

    import fsl.data.image as fslimage

    # The fsleyes.overlay.loadOverlay function
    # works asynchronously - this function will
    # get called once all of the overlays have
    # been loaded.
    def onLoad(overlays):

        overlayList.extend(overlays)

        for i, overlay in enumerate(overlayList):

            status.update('Applying display settings '
                          'to {}...'.format(overlay.name))

            display = displayCtx.getDisplay(overlay)
            optArgs = args.overlays[i]

            delattr(optArgs, 'overlay')

            # Figure out how many arguments
            # were passed in for this overlay

            allArgs = [v for k, v in vars(optArgs).items()
                       if k != 'overlayType']
            nArgs   = len([a for a in allArgs if a is not None])

            # If no arguments were passed,
            # apply default display settings
            if nArgs == 0 and args.autoDisplay:
                autodisplay.autoDisplay(overlay, overlayList, displayCtx)
                continue

            # Otherwise, we start by applying
            # arguments to the Display instance
            _applyArgs(optArgs, display)

            # Retrieve the DisplayOpts instance
            # after applying arguments to the
            # Display instance - if the overlay
            # type is set on the command line, the
            # DisplayOpts instance will have been
            # re-created
            opts = display.getDisplayOpts()

            # All options in the FILE_OPTIONS dictionary
            # are Choice properties, where the valid
            # choices are defined by the current
            # contents of the overlay list. So when
            # the user specifies one of these images,
            # we need to do an explicit check to see
            # if the specified image is valid.
            # 
            # Here, I'm loading the image, and checking
            # to see if it can be used to modulate the
            # vector image (just with a dimension check).
            # If it can, I add it to the image list - the
            # applyArguments function will apply the
            # value. If the modulate file is not valid,
            # an error is raised.
            fileOpts = FILE_OPTIONS.get(opts, [])

            for fileOpt in fileOpts:
                value = getattr(optArgs, fileOpt)
                if value is not None:

                    image = _findOrLoad(overlayList,
                                        value,
                                        fslimage.Image,
                                        overlay)

                    setattr(optArgs, fileOpt, None)

                    # If the user specified both clipImage
                    # arguments and linklow/high range
                    # arguments, an error will be raised
                    # when we try to set the link properties
                    # on the VolumeOpts instance (because
                    # they have been disabled). So we
                    # clear themfrom the argparse namespace
                    # to prevent this from occurring.
                    if fileOpt == 'clipImage' and \
                       isinstance(opts, fsldisplay.VolumeOpts):

                        llr = ARGUMENTS['VolumeOpts.linkLowRanges'][ 1]
                        lhr = ARGUMENTS['VolumeOpts.linkHighRanges'][1]

                        setattr(optArgs, llr, None)
                        setattr(optArgs, lhr, None)

                    # With the exception of ModelOpts.refImage,
                    # all of the file options specify images which
                    # must match the overlay shape to be valid.
                    if not isinstance(opts, fsldisplay.ModelOpts):
                        if image.shape[:3] != overlay.shape[:3]:
                            log.warn('{}: Shape of {} ({}) does not '
                                     'match shape of {} ({})'.format(
                                         fileOpt,
                                         overlay,
                                         overlay.shape[:3],
                                         image,
                                         image.shape[:3]))
                            continue

                    setattr(opts, fileOpt, image)

            # After handling the special cases
            # above, we can apply the CLI
            # options to the Opts instance
            _applyArgs(optArgs, opts)

    paths = [o.overlay for o in args.overlays]

    if len(paths) > 0:
        fsloverlay.loadOverlays(paths, onLoad=onLoad, **kwargs)
 
        
def _findOrLoad(overlayList, overlayFile, overlayType, relatedTo=None):
    """Searches for the given ``overlayFile`` in the ``overlayList``. If not
    present, it is created using the given ``overlayType`` constructor, and
    inserted into the ``overlayList``. The new overlay is inserted into the
    ``overlayList`` before the ``relatedTo`` overlay if provided, otherwise
    appended to the end of the list.
    """

    overlayFile = op.abspath(overlayFile)
    overlay     = overlayList.find(overlayFile)

    if overlay is None:

        overlay = overlayType(overlayFile)

        if relatedTo is not None:
            overlayList.insert(overlayList.index(relatedTo), overlay)
        else:
            overlayList.append(overlay)

    return overlay