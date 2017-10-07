#!/usr/bin/env python
#
# volume3dopts.py - The Volume3DOpts class.
#
# Author: Paul McCarthy <pauldmccarthy@gmail.com>
#
"""This module provides the :class:`.Volume3DOpts` class, a mix-in for
use with :class:`.DisplayOpts` classes.
"""


import numpy as np

from   fsl.utils.platform import platform as fslplatform
import fsl.utils.transform                as transform
import fsleyes_props                      as props


class Volume3DOpts(object):
    """The ``Volume3DOpts`` class is a mix-in for use with :class:`.DisplayOpts`
    classes. It defines display properties used for ray-cast based rendering
    of :class:`.Image` overlays.


    The properties in this class are tightly coupled to the ray-casting
    implementation used by the :class:`.GLVolume` class - see its documentation
    for details.
    """


    dithering = props.Real(minval=0,
                           maxval=0.05,
                           default=0.01,
                           clamped=True)
    """Specifies the amount of randomness to introduce in the rendering
    procedure to achieve a dithering (addition of random noise) effect. This is
    necessary to remove some aliasing effects inherent in the rendering
    process.
    """


    blendFactor = props.Real(minval=0.001, maxval=1, default=0.2)
    """Controls how much each sampled point on each ray contributes to the
    final colour.
    """


    numSteps = props.Int(minval=25, maxval=500, default=100, clamped=False)
    """Specifies the maximum number of samples to acquire in the rendering of
    each pixel of the 3D scene. This corresponds to the number of iterations
    of the ray-casting loop.

    .. note:: In a low performance environment, the actual number of steps
              may differ from this value - use the :meth:`getNumSteps` method
              to get the number of steps that are actually executed.
    """


    numInnerSteps = props.Int(minval=1, maxval=100, default=10, clamped=True)
    """Only used in low performance environments. Specifies the number of
    ray-casting steps to execute in a single iteration on the GPU, as part
    of an outer loop which is running on the CPU. See the :class:`.GLVolume`
    class documentation for more details on the rendering process.

    .. warning:: The maximum number of iterations that can be performed within
                 an ARB fragment program is implementation-dependent. Too high
                 a value may result in errors or a corrupted view. See the
                 :class:`.GLVolume` class for details.
    """


    resolution = props.Int(minval=10, maxval=100, default=100, clamped=True)
    """Only used in low performance environments. Specifies the resolution
    of the off-screen buffer to which the volume is rendered, as a percentage
    of the screen resolution.

    See the :class:`.GLVolume` class documentation for more details.
    """


    numClipPlanes = props.Int(minval=0, maxval=10, default=0, clamped=True)
    """Number of active clip planes. """


    showClipPlanes = props.Boolean(default=False)
    """If ``True``, wirframes depicting the active clipping planes will
    be drawn.
    """


    clipPosition = props.List(
        props.Percentage(minval=0, maxval=100, clamped=True),
        minlen=10,
        maxlen=10)
    """Centre of clip-plane rotation, as a distance from the volume centre -
    0.5 is centre.
    """


    clipAzimuth = props.List(
        props.Real(minval=-180, maxval=180, clamped=True),
        minlen=10,
        maxlen=10)
    """Rotation (degrees) of the clip plane about the Z axis, in the display
    coordinate system.
    """


    clipInclination = props.List(
        props.Real(minval=-180, maxval=180, clamped=True),
        minlen=10,
        maxlen=10)
    """Rotation (degrees) of the clip plane about the Y axis in the display
    coordinate system.
    """


    def __init__(self):
        """Create a :class:`Volume3DOpts` instance.
        """

        # If we're in an X11/SSh session,
        # step down the quality so it's
        # a bit faster.
        if fslplatform.inSSHSession:
            self.numSteps    = 40
            self.resolution  = 40
            self.dithering   = 0.02
            self.blendFactor = 0.4

        self.clipPosition[:]    = 10 * [50]
        self.clipAzimuth[:]     = 10 * [0]
        self.clipInclination[:] = 10 * [0]

        # Give convenient initial values for
        # the first three clipping planes
        self.clipInclination[1] = 90
        self.clipAzimuth[    1] = 0
        self.clipInclination[2] = 90
        self.clipAzimuth[    2] = 90


    def destroy(self):
        """
        """
        pass


    def getNumSteps(self):
        """Return the value of the :attr:`numSteps` property, possibly
        adjusted according to the the :attr:`numInnerSteps` property. The
        result of this method should be used instead of the value of
        the :attr:`numSteps` property.

        See the :class:`.GLVolume` class for more details.
        """

        if float(fslplatform.glVersion) >= 2.1:
            return self.numSteps

        outer = self.getNumOuterSteps()

        return int(outer * self.numInnerSteps)


    def getNumOuterSteps(self):
        """Returns the number of iterations for the outer ray-casting loop.

        See the :class:`.GLVolume` class for more details.
        """

        total = self.numSteps
        inner = self.numInnerSteps
        outer = np.ceil(total / float(inner))

        return int(outer)


    def calculateRayCastSettings(self, view=None, proj=None):
        """Calculates various parameters required for 3D ray-cast rendering
        (see the :class:`.GLVolume` class).


        :arg view: Transformation matrix which transforms from model
                   coordinates to view coordinates (i.e. the GL view matrix).


        :arg proj: Transformation matrix which transforms from view coordinates
                   to normalised device coordinates (i.e. the GL projection
                   matrix).

        Returns a tuple containing:

          - A vector defining the amount by which to move along a ray in a
            single iteration of the ray-casting algorithm. This can be added
            directly to the volume texture coordinates.

          - A vector defining the maximum distance by which to randomly adjust
            the start location of each ray, to induce a dithering effect in
            the rendered scene.

          - A transformation matrix which transforms from image texture
            coordinates into the display coordinate system.

        .. note:: This method will raise an error if called on a
                  ``GLImageObject`` which is managing an overlay that is not
                  associated with a :class:`.Volume3DOpts` instance.
        """

        if view is None: view = np.eye(4)
        if proj is None: proj = np.eye(4)

        # In GL, the camera position
        # is initially pointing in
        # the -z direction.
        eye    = [0, 0, -1]
        target = [0, 0,  1]

        # We take this initial camera
        # configuration, and transform
        # it by the inverse modelview
        # matrix
        t2dmat = self.getTransform('texture', 'display')
        xform  = transform.concat(view, t2dmat)
        ixform = transform.invert(xform)

        eye    = transform.transform(eye,    ixform, vector=True)
        target = transform.transform(target, ixform, vector=True)

        # Direction that the 'camera' is
        # pointing, normalied to unit length
        cdir = transform.normalise(eye - target)

        # Calculate the length of one step
        # along the camera direction in a
        # single iteration of the ray-cast
        # loop. Multiply by sqrt(3) so that
        # the maximum number of steps will
        # be reached across the longest axis
        # of the image texture cube.
        rayStep = np.sqrt(3) * cdir / self.getNumSteps()

        # Maximum amount by which to dither
        # the scene. This is done by applying
        # a random offset to the starting
        # point of each ray - we pass the
        # shader a vector in the camera direction,
        # so all it needs to do is scale the
        # vector by a random amount, and add the
        # vector to the starting point.
        ditherDir = cdir * self.dithering

        # A transformation matrix which can
        # transform image texture coordinates
        # into the corresponding screen
        # (normalised device) coordinates.
        # This allows the fragment shader to
        # convert an image texture coordinate
        # into a relative depth value.
        #
        # The projection matrix puts depth into
        # [-1, 1], but we want it in [0, 1]
        zscale = transform.scaleOffsetXform([1, 1, 0.5], [0, 0, 0.5])
        xform  = transform.concat(zscale, proj, xform)

        return rayStep, ditherDir, xform


    def get3DClipPlane(self, planeIdx):
        """A convenience method which calculates a point-vector description
        of the specified clipping plane. ``planeIdx`` is an index into the
        :attr:`clipPosition`, :attr:`clipAzimuth`, and
        :attr:`clipInclination`, properties.

        Returns the clip plane at the given ``planeIdx`` as an origin and
        normal vector, in the display coordinate system..
        """

        pos     = self.clipPosition[   planeIdx]
        azimuth = self.clipAzimuth[    planeIdx]
        incline = self.clipInclination[planeIdx]

        b       = self.bounds
        pos     = pos             / 100.0
        azimuth = azimuth * np.pi / 180.0
        incline = incline * np.pi / 180.0

        xmid = b.xlo + 0.5 * b.xlen
        ymid = b.ylo + 0.5 * b.ylen
        zmid = b.zlo + 0.5 * b.zlen

        centre = [xmid, ymid, zmid]
        normal = [0, 0, -1]

        rot1     = transform.axisAnglesToRotMat(incline, 0, 0)
        rot2     = transform.axisAnglesToRotMat(0, 0, azimuth)
        rotation = transform.concat(rot2, rot1)

        normal = transform.transformNormal(normal, rotation)
        normal = transform.normalise(normal)

        offset = (pos - 0.5) * max((b.xlen, b.ylen, b.zlen))
        origin = centre + normal * offset

        return origin, normal