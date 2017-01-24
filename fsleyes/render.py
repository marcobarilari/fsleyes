#!/usr/bin/env python
#
# render.py - Generate screenshots of overlays using OpenGL.
#
# Author: Paul McCarthy <pauldmccarthy@gmail.com>
#
"""The ``render`` module is a program which provides off-screen rendering
capability for scenes which can otherwise be displayed via *FSLeyes*.
"""


import sys
import logging
import textwrap
import argparse

import props

import fsl.utils.layout                    as fsllayout
import fsl.utils.colourbarbitmap           as cbarbitmap
import fsl.utils.textbitmap                as textbitmap
import fsl.data.constants                  as constants
import                                        fsleyes
import fsleyes.main                        as fsleyesmain
import fsleyes.version                     as version
import fsleyes.strings                     as strings
import fsleyes.overlay                     as fsloverlay
import fsleyes.colourmaps                  as fslcm
import fsleyes.parseargs                   as parseargs
import fsleyes.displaycontext              as displaycontext
import fsleyes.displaycontext.orthoopts    as orthoopts
import fsleyes.displaycontext.lightboxopts as lightboxopts
import fsleyes.gl                          as fslgl
import fsleyes.gl.offscreenslicecanvas     as slicecanvas
import fsleyes.gl.offscreenlightboxcanvas  as lightboxcanvas


log = logging.getLogger(__name__)


CBAR_SIZE  = 75
"""Height/width, in pixels, of a colour bar. """


LABEL_SIZE = 20
"""Height/width, in pixels, of an orientation label. """


def main(args=None):
    """Entry point for ``render``.

    Creates and renders an OpenGL scene, and saves it to a file, according
    to the specified command line arguments (which default to
    ``sys.argv[1:]``).
    """

    if args is None:
        args = sys.argv[1:]

    # Initialise OpenGL
    fslgl.getGLContext(offscreen=True, createApp=True)
    fslgl.bootstrap() 

    # Initialise FSLeyes and colour 
    # maps, and implement hacks
    fsleyes.initialise()
    fsleyesmain.hacksAndWorkarounds()
    
    fslcm.init()

    # Parse arguments, and
    # configure logging/debugging
    namespace = parseArgs(args)
    fsleyes.configLogging(namespace)

    # Create a description of the scene
    overlayList, displayCtx, sceneOpts = makeDisplayContext(namespace)

    import matplotlib.image as mplimg

    # Render that scene, and save it to file
    bitmap = render(namespace, overlayList, displayCtx, sceneOpts)
    mplimg.imsave(namespace.outfile, bitmap)

    
def parseArgs(argv):
    """Creates an argument parser which accepts options for off-screen
    rendering. Uses the :mod:`fsleyes.parseargs` module to peform the
    actual parsing.

    :returns: An ``argparse.Namespace`` object containing the parsed
              arguments.
    """

    mainParser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=parseargs.FSLeyesHelpFormatter)

    mainParser.add_argument('-of',
                            '--outfile',
                            help='Output image file name'),
    mainParser.add_argument('-sz',
                            '--size',
                            type=int, nargs=2,
                            metavar=('W', 'H'),
                            help='Size in pixels (width, height)',
                            default=(800, 600))
    mainParser.add_argument('-o',
                            '--selectedOverlay',
                            metavar='IDX',
                            help='Index of selected overlay '
                                 '(starting from 0)'), 

    name        = 'render'
    prolog      = 'FSLeyes render version {}\n'.format(version.__version__)
    optStr      = '-of outfile'
    description = textwrap.dedent("""\
        FSLeyes screenshot generator.

        Use the '--scene' option to choose between orthographic
        ('ortho') or lightbox ('lightbox') view.
        """)
    
    namespace = parseargs.parseArgs(mainParser,
                                    argv,
                                    name,
                                    prolog=prolog,
                                    desc=description,
                                    usageProlog=optStr,
                                    argOpts=['of', 'outfile', 'sz', 'size'],
                                    shortHelpExtra=['--outfile', '--size'])

    if namespace.outfile is None:
        log.error('outfile is required')
        mainParser.print_usage()
        sys.exit(1)

    if namespace.scene not in ('ortho', 'lightbox'):
        log.info('Unknown scene specified  ("{}") - defaulting '
                 'to ortho'.format(namespace.scene))
        namespace.scene = 'ortho'
 
    return namespace


def makeDisplayContext(namespace):
    """Creates :class:`.OverlayList`, :class:`.DisplayContext``, and
    :class:`.SceneOpts` instances which represent the scene to be rendered,
    as described by the arguments in the given ``namespace`` object.
    """

    # Create an overlay list and display context.
    # The DisplayContext, Display and DisplayOpts
    # classes are designed to be created in a
    # parent-child hierarchy. So we need to create
    # a 'dummy' master display context to make
    # things work properly.
    overlayList      = fsloverlay.OverlayList()
    masterDisplayCtx = displaycontext.DisplayContext(overlayList)
    childDisplayCtx  = displaycontext.DisplayContext(overlayList,
                                                     parent=masterDisplayCtx)

    # The handleOverlayArgs function uses the
    # fsleyes.overlay.loadOverlays function,
    # which will call these functions as it
    # goes through the list of overlay to be
    # loaded.
    def load(ovl):
        log.info('Loading overlay {} ...'.format(ovl))
        
    def error(ovl, error):
        log.info('Error loading overlay {}: '.format(ovl, error))

    # Load the overlays specified on the command
    # line, and configure their display properties
    parseargs.applyOverlayArgs(namespace,
                               overlayList,
                               masterDisplayCtx,
                               loadFunc=load,
                               errorFunc=error)

    # Create a SceneOpts instance describing
    # the scene to be rendered
    if   namespace.scene == 'ortho':    sceneOpts = orthoopts   .OrthoOpts()
    elif namespace.scene == 'lightbox': sceneOpts = lightboxopts.LightBoxOpts()

    parseargs.applySceneArgs(namespace,
                             overlayList,
                             childDisplayCtx,
                             sceneOpts)

    # This has to be applied after applySceneArgs,
    # in case the user used the '-std'/'-std1mm'
    # options.
    if namespace.selectedOverlay is not None:
        masterDisplayCtx.selectedOverlay = namespace.selectedOverlay

    if len(overlayList) == 0:
        raise RuntimeError('At least one overlay must be specified')

    return overlayList, childDisplayCtx, sceneOpts


def render(namespace, overlayList, displayCtx, sceneOpts):
    """Renders the scene, and returns a bitmap.

    :arg namespace:   ``argparse.Namespace`` object containing command line
                      arguments.

    :arg overlayList: The :class:`.OverlayList` instance.
    :arg displayCtx:  The :class:`.DisplayContext` instance.
    :arg sceneOpts:   The :class:`.SceneOpts` instance.
    """
    
    # Calculate canvas and colour bar sizes
    # so that the entire scene will fit in
    # the width/height specified by the user
    width, height = namespace.size
    (width, height), (cbarWidth, cbarHeight) = \
        adjustSizeForColourBar(width,
                               height,
                               sceneOpts.showColourBar,
                               sceneOpts.colourBarLocation)

    # Lightbox view -> only one canvas
    if namespace.scene == 'lightbox':
        c = createLightBoxCanvas(namespace,
                                 width,
                                 height,
                                 overlayList,
                                 displayCtx,
                                 sceneOpts)
        canvases = [c]

    # Ortho view -> up to three canvases
    elif namespace.scene == 'ortho':
        canvases = createOrthoCanvases(namespace,
                                       width,
                                       height,
                                       overlayList,
                                       displayCtx,
                                       sceneOpts)

    # Do we need to do a neuro/radio l/r flip?
    inRadio = displayCtx.displaySpaceIsRadiological()
    lrFlip  = displayCtx.radioOrientation != inRadio

    if lrFlip:
        for c in canvases:
            if c.zax in (1, 2):
                c.invertX = True

    # fix orthographic projection if
    # showing an ortho grid layout.
    # Note that, if the user chose 'grid',
    # but also chose to hide one or more
    # canvases, the createOrthoCanvases
    # function will have adjusted the
    # value of sceneOpts.layout. So
    # if layout == grid, we definitely
    # have three canvases.
    #
    # The createOrthoCanvases also
    # re-orders the canvases, which
    # we're assuming knowledge of,
    # by indexing canvases[1].
    if namespace.scene == 'ortho' and sceneOpts.layout == 'grid':
        canvases[1].invertX = True

    # Configure each of the canvases (with those
    # properties that are common to both ortho and
    # lightbox canvases) and render them one by one
    canvasBmps = []
    for i, c in enumerate(canvases):

        if   c.zax == 0: c.pos.xyz = displayCtx.location.yzx
        elif c.zax == 1: c.pos.xyz = displayCtx.location.xzy
        elif c.zax == 2: c.pos.xyz = displayCtx.location.xyz

        c.draw()

        canvasBmps.append(c.getBitmap())

    # Show/hide orientation labels -
    # not supported on lightbox view
    if namespace.scene == 'lightbox' or not sceneOpts.showLabels:
        labelBmps = None
    else:
        labelBmps = buildLabelBitmaps(overlayList,
                                      displayCtx,
                                      canvases,
                                      canvasBmps,
                                      sceneOpts.bgColour[:3],
                                      sceneOpts.bgColour[ 3])

    # layout the bitmaps
    if namespace.scene == 'lightbox':
        layout = fsllayout.Bitmap(canvasBmps[0])
    else:
        layout = fsllayout.buildOrthoLayout(canvasBmps,
                                            labelBmps,
                                            sceneOpts.layout,
                                            sceneOpts.showLabels,
                                            LABEL_SIZE)

    # Render a colour bar if required
    if sceneOpts.showColourBar:
        cbarBmp = buildColourBarBitmap(overlayList,
                                       displayCtx,
                                       cbarWidth,
                                       cbarHeight,
                                       sceneOpts.colourBarLocation,
                                       sceneOpts.colourBarLabelSide,
                                       sceneOpts.bgColour)
        if cbarBmp is not None:
            layout  = buildColourBarLayout(layout,
                                           cbarBmp,
                                           sceneOpts.colourBarLocation,
                                           sceneOpts.colourBarLabelSide)

    # Turn the layout tree into a bitmap image
    return fsllayout.layoutToBitmap(
        layout, [c * 255 for c in sceneOpts.bgColour])


def createLightBoxCanvas(namespace,
                         width,
                         height, 
                         overlayList,
                         displayCtx,
                         sceneOpts):
    """Creates, configures, and returns an :class:`.OffScreenLightBoxCanvas`.

    :arg namespace:   ``argparse.Namespace`` object.
    :arg width:       Available width in pixels.
    :arg height:      Available height in pixels.
    :arg overlayList: The :class:`.OverlayList` instance. 
    :arg displayCtx:  The :class:`.DisplayContext` instance.
    :arg sceneOpts:   The :class:`.SceneOpts` instance.
    """
    
    canvas = lightboxcanvas.OffScreenLightBoxCanvas(
        overlayList,
        displayCtx,
        zax=sceneOpts.zax,
        width=width,
        height=height)

    props.applyArguments(canvas, namespace)
    
    return canvas


def createOrthoCanvases(namespace,
                        width,
                        height, 
                        overlayList,
                        displayCtx,
                        sceneOpts):
    """Creates, configures, and returns up to three
    :class:`.OffScreenSliceCanvas` instances, for rendering the scene.

    :arg namespace:   ``argparse.Namespace`` object.
    :arg width:       Available width in pixels.
    :arg height:      Available height in pixels.
    :arg overlayList: The :class:`.OverlayList` instance. 
    :arg displayCtx:  The :class:`.DisplayContext` instance.
    :arg sceneOpts:   The :class:`.SceneOpts` instance. 
    """

    canvases = []
    
    xc, yc, zc = parseargs.calcCanvasCentres(namespace,
                                             overlayList,
                                             displayCtx) 

    # Build a list containing the horizontal 
    # and vertical axes for each canvas
    canvasAxes = []
    zooms      = []
    centres    = []
    if sceneOpts.showXCanvas:
        canvasAxes.append((1, 2))
        zooms     .append(sceneOpts.xzoom)
        centres   .append(xc)
    if sceneOpts.showYCanvas:
        canvasAxes.append((0, 2))
        zooms     .append(sceneOpts.yzoom)
        centres   .append(yc)
    if sceneOpts.showZCanvas:
        canvasAxes.append((0, 1))
        zooms     .append(sceneOpts.zzoom)
        centres   .append(zc)

    # Grid layout only makes sense if
    # we're displaying 3 canvases
    if sceneOpts.layout == 'grid' and len(canvasAxes) <= 2:
        sceneOpts.layout = 'horizontal'

    if sceneOpts.layout == 'grid':
        canvasAxes = [canvasAxes[1], canvasAxes[0], canvasAxes[2]]
        centres    = [centres[   1], centres[   0], centres[   2]]
        zooms      = [zooms[     1], zooms[     0], zooms[     2]]

    # Calculate the size in pixels for each canvas
    sizes = calculateOrthoCanvasSizes(overlayList,
                                      displayCtx,
                                      width,
                                      height,
                                      canvasAxes,
                                      sceneOpts.showLabels,
                                      sceneOpts.layout)

    # Configure the properties on each canvas
    for ((width, height), (xax, yax), zoom, centre) in zip(sizes,
                                                           canvasAxes,
                                                           zooms,
                                                           centres):

        zax = 3 - xax - yax

        if centre is None:
            centre = (displayCtx.location[xax], displayCtx.location[yax])

        c = slicecanvas.OffScreenSliceCanvas(
            overlayList,
            displayCtx,
            zax=zax,
            width=int(width),
            height=int(height))

        c.showCursor      = sceneOpts.showCursor
        c.cursorColour    = sceneOpts.cursorColour
        c.bgColour        = sceneOpts.bgColour
        c.renderMode      = sceneOpts.renderMode
        c.resolutionLimit = sceneOpts.resolutionLimit

        if zoom is not None: c.zoom = zoom
        c.centreDisplayAt(*centre)
        canvases.append(c)

    return canvases


def buildLabelBitmaps(overlayList,
                      displayCtx,
                      canvases, 
                      canvasBmps,
                      bgColour,
                      alpha):
    """Creates bitmaps containing anatomical orientation labels.

    :arg overlayList: The :class:`.OverlayList`.
    
    :arg displayCtx:  The :class:`.DisplayContext`.
    
    :arg canvases:    The :class:`.SliceCanvas` objects which need labels.
    
    :arg canvasBmps:  A sequence of bitmaps, one for each canvas.
    
    :arg bgColour:    RGB background colour (values between ``0`` and ``1``).
    
    :arg alpha:       Transparency.(between ``0`` and ``1``).

    :returns:         A list of dictionaries, one dictionary for each canvas.  
                      Each dictionary contains ``{label -> bitmap}`` mappings,
                      where ``label`` is either ``top``, ``bottom``, ``left``
                      or ``right``.
    """
    
    # Default label colour is determined from the background
    # colour. If the orientation labels cannot be determined
    # though, the foreground colour will be changed to red.
    fgColour = fslcm.complementaryColour(bgColour)

    overlay = displayCtx.getReferenceImage(displayCtx.getSelectedOverlay())

    # There's no reference image for the selected overlay,
    # so we cannot calculate orientation labels
    if overlay is None:
        xorient = constants.ORIENT_UNKNOWN
        yorient = constants.ORIENT_UNKNOWN
        zorient = constants.ORIENT_UNKNOWN
    else:

        display = displayCtx.getDisplay(overlay)
        opts    = display.getDisplayOpts()
        xform   = opts.getTransform('world', 'display')
        xorient = overlay.getOrientation(0, xform)
        yorient = overlay.getOrientation(1, xform)
        zorient = overlay.getOrientation(2, xform)

    if constants.ORIENT_UNKNOWN in [xorient, yorient, zorient]:
        fgColour = 'red'

    xlo = strings.anatomy['Nifti', 'lowshort',  xorient]
    ylo = strings.anatomy['Nifti', 'lowshort',  yorient]
    zlo = strings.anatomy['Nifti', 'lowshort',  zorient]
    xhi = strings.anatomy['Nifti', 'highshort', xorient]
    yhi = strings.anatomy['Nifti', 'highshort', yorient]
    zhi = strings.anatomy['Nifti', 'highshort', zorient]

    loLabels = [xlo, ylo, zlo]
    hiLabels = [xhi, yhi, zhi]

    labelBmps = []

    for canvas, canvasBmp in zip(canvases, canvasBmps):

        xax, yax = canvas.xax,    canvas.yax
        lox, hix = loLabels[xax], hiLabels[xax]
        loy, hiy = loLabels[yax], hiLabels[yax]

        if canvas.invertX: lox, hix = hix, lox
        if canvas.invertY: loy, hiy = hiy, loy

        width        = canvasBmp.shape[1]
        height       = canvasBmp.shape[0]

        allLabels    = {}
        labelKeys    = ['left',     'right',    'top',      'bottom']
        labelTexts   = [lox,        hix,        loy,        hiy]
        labelWidths  = [LABEL_SIZE, LABEL_SIZE, width,      width]
        labelHeights = [height,     height,     LABEL_SIZE, LABEL_SIZE]


        for key, text, width, height in zip(labelKeys,
                                            labelTexts,
                                            labelWidths,
                                            labelHeights):

            allLabels[key] = textbitmap.textBitmap(
                text=text,
                width=width,
                height=height,
                fontSize=12,
                fgColour=fgColour,
                bgColour=bgColour,
                alpha=alpha)

        labelBmps.append(allLabels)
            
    return labelBmps


def buildColourBarBitmap(overlayList,
                         displayCtx,
                         width,
                         height,
                         cbarLocation,
                         cbarLabelSide,
                         bgColour):
    """If the currently selected overlay has a display range,
    creates and returns a bitmap containing a colour bar. Returns
    ``None`` otherwise.

    :arg overlayList:   The :class:`.OverlayList`.
    
    :arg displayCtx:    The :class:`.DisplayContext`.
    
    :arg width:         Colour bar width in pixels.
    
    :arg height:        Colour bar height in pixels.
    
    :arg cbarLocation:  One of  ``'top'``, ``'bottom'``, ``'left'``, or
                        ``'right'``.
    
    :arg cbarLabelSide: One of ``'top-left'`` or ``'bottom-right'``.
    
    :arg bgColour:      RGBA background colour.
    """

    overlay = displayCtx.getSelectedOverlay()
    display = displayCtx.getDisplay(overlay)
    opts    = display.getDisplayOpts()

    # TODO Support other overlay types which
    # have a display range (when they exist).
    if not isinstance(opts, displaycontext.VolumeOpts):
        return None
    
    if   cbarLocation in ('top', 'bottom'): orient = 'horizontal'
    elif cbarLocation in ('left', 'right'): orient = 'vertical'
    
    if   cbarLabelSide == 'top-left':
        if orient == 'horizontal': labelSide = 'top'
        else:                      labelSide = 'left'
    elif cbarLabelSide == 'bottom-right':
        if orient == 'horizontal': labelSide = 'bottom'
        else:                      labelSide = 'right'


    if opts.useNegativeCmap:
        negCmap    = opts.negativeCmap
        ticks      = [0.0, 0.49, 0.51, 1.0]
        ticklabels = ['{:0.2f}'.format(-opts.displayRange.xhi),
                      '{:0.2f}'.format(-opts.displayRange.xlo),
                      '{:0.2f}'.format( opts.displayRange.xlo),
                      '{:0.2f}'.format( opts.displayRange.xhi)]
        tickalign  = ['left', 'right', 'left', 'right']
    else:
        negCmap    = None
        ticks      = [0.0, 1.0]
        tickalign  = ['left', 'right']
        ticklabels = ['{:0.2f}'.format(opts.displayRange.xlo),
                      '{:0.2f}'.format(opts.displayRange.xhi)]

    cbarBmp = cbarbitmap.colourBarBitmap(
        cmap=opts.cmap,
        width=width,
        height=height, 
        negCmap=negCmap,
        invert=opts.invert,
        ticks=ticks,
        ticklabels=ticklabels,
        tickalign=tickalign,
        label=display.name,
        orientation=orient,
        labelside=labelSide,
        bgColour=bgColour,
        textColour=fslcm.complementaryColour(bgColour),
        cmapResolution=opts.cmapResolution)

    # The colourBarBitmap function returns a w*h*4
    # array, but the fsl.utils.layout.Bitmap (see
    # the next function) assumes a h*w*4 array
    cbarBmp = cbarBmp.transpose((1, 0, 2))
    
    return cbarBmp

 
def buildColourBarLayout(canvasLayout,
                         cbarBmp,
                         cbarLocation,
                         cbarLabelSide):
    """Given a layout object containing the rendered canvas bitmaps,
    creates a new layout which incorporates the given colour bar bitmap.

    :arg canvasLayout:  An object describing the canvas layout (see
                        :mod:`fsl.utils.layout`)
    
    :arg cbarBmp:       A bitmap containing a rendered colour bar.
    
    :arg cbarLocation:  Colour bar location (see :func:`buildColourBarBitmap`).
    
    :arg cbarLabelSide: Colour bar label side (see
                        :func:`buildColourBarBitmap`).
    """

    cbarBmp = fsllayout.Bitmap(cbarBmp)

    if   cbarLocation in ('top',    'left'):  items = [cbarBmp, canvasLayout]
    elif cbarLocation in ('bottom', 'right'): items = [canvasLayout, cbarBmp]

    if   cbarLocation in ('top', 'bottom'): return fsllayout.VBox(items)
    elif cbarLocation in ('left', 'right'): return fsllayout.HBox(items)


def adjustSizeForColourBar(width, height, showColourBar, colourBarLocation):
    """Calculates the widths and heights of the image display space, and the
    colour bar if it is enabled.

    :arg width:             Desired width in pixels
    
    :arg height:            Desired height in pixels
    
    :arg showColourBar:     ``True`` if a colour bar is to be shown, ``False``
                            otherwise.
    
    :arg colourBarLocation: Colour bar location (see
                            :func:`buildColourBarBitmap`).
    
    :returns:               Two tuples - the first tuple contains the
                            ``(width, height)`` of the available canvas space, 
                            and the second contains the ``(width, height)`` of 
                            the colour bar.
    """

    if showColourBar:

        cbarWidth = CBAR_SIZE
        if colourBarLocation in ('top', 'bottom'):
            height     = height - cbarWidth
            cbarHeight = cbarWidth
            cbarWidth  = width
        else:
            width      = width  - cbarWidth
            cbarHeight = height
    else:
        cbarWidth  = 0
        cbarHeight = 0

    return (width, height), (cbarWidth, cbarHeight)


def calculateOrthoCanvasSizes(overlayList,
                              displayCtx,
                              width,
                              height,
                              canvasAxes,
                              showLabels,
                              layout):
    """Calculates the sizes, in pixels, for each canvas to be displayed in an
    orthographic layout.

    :arg overlayList: The :class:`.OverlayList`.
    
    :arg displayCtx:  The :class:`.DisplayContext`.
    
    :arg width:       Available width in pixels.
    
    :arg height:      Available height in pixels.
    
    :arg canvasAxes:  A sequence of ``(xax, yax)`` indices, one for each
                      bitmap in ``canvasBmps``.
    
    :arg showLabels:  ``True`` if orientation labels are to be shown.
    
    :arg layout:      Either ``'horizontal'``, ``'vertical'``, or ``'grid'``,
                      describing the canvas layout.

    :returns:         A list of ``(width, height)`` tuples, one for each 
                      canvas, each specifying the canvas width and height in 
                      pixels. 
    """

    bounds   = displayCtx.bounds
    axisLens = [bounds.xlen, bounds.ylen, bounds.zlen]

    # Grid layout only makes sense if we're
    # displaying all three canvases
    if layout == 'grid' and len(canvasAxes) <= 2:
        raise ValueError('Grid layout only supports 3 canvases')

    # If we're displaying orientation labels,
    # reduce the available width and height
    # by a fixed amount
    if showLabels:
        if layout == 'horizontal':
            width  -= 2 * LABEL_SIZE * len(canvasAxes)
            height -= 2 * LABEL_SIZE
        elif layout == 'vertical':
            width  -= 2 * LABEL_SIZE
            height -= 2 * LABEL_SIZE * len(canvasAxes)
        elif layout == 'grid':
            width  -= 4 * LABEL_SIZE
            height -= 4 * LABEL_SIZE

    # Distribute the height across canvas heights
    return fsllayout.calcSizes(layout,
                               canvasAxes,
                               axisLens,
                               width,
                               height)


if __name__ == '__main__':
    main()
