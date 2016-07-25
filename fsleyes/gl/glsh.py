#!/usr/bin/env python
#
# glsh.py - The GLSH class.
#
# Author: Paul McCarthy <pauldmccarthy@gmail.com>
#
"""This module provides the :class:`GLSH` class, a :class:`.GLObject` for
rendering :class:`.Image` overlays which contain spherical harmonic diffusion
estimates. The ``GLSH`` class uses functions defined in the
:mod:`.gl21.glsh_funcs` module.

:class:`GLSH` instances can only be rendered in OpenGL 2.1 and above.
"""


import               logging

import numpy      as np

import OpenGL.GL  as gl


import fsleyes.gl          as fslgl
import fsleyes.colourmaps  as fslcm
import fsleyes.gl.textures as textures
from . import                 globject


log = logging.getLogger(__name__)


class GLSH(globject.GLImageObject):
    """

Creates a :class:`.Texture3D` instance for storing radius values, and
    then 

    ``radTexture``     ``gl.GL_TEXTURE0``
    ``cmapTexture``    ``gl.GL_TEXTURE1``
    """

    def __init__(self, image, display, xax, yax):
        """
        """
        
        globject.GLImageObject.__init__(self, image, display, xax, yax)

        name = self.name

        self.shader      = None
        self.radTexture  = textures.Texture3D('{}_radTexture'.format(name),
                                              threaded=False)
        self.cmapTexture = textures.ColourMapTexture('{}_cm'.format(name))

        self.addListeners()
        self.shResChanged()
        self.cmapUpdate()
        
        fslgl.glsh_funcs.init(self)

        
    def destroy(self):
        
        self.removeListeners()

        fslgl.glsh_funcs.destroy(self)

        if self.radTexture  is not None: self.radTexture.destroy()
        if self.cmapTexture is not None: self.cmapTexture.destroy() 

        self.radTexture  = None
        self.cmapTexture = None


    def addListeners(self):

        display = self.display
        opts    = self.displayOpts
        name    = self.name

        opts   .addListener('resolution',      name, self.notify)
        opts   .addListener('transform',       name, self.notify)
        
        opts   .addListener('shResolution' ,   name, self.shResChanged,
                            immediate=True)
        opts   .addListener('size',            name, self.updateShaderState)
        opts   .addListener('lighting',        name, self.updateShaderState)
        opts   .addListener('neuroFlip',       name, self.updateShaderState)
        opts   .addListener('radiusThreshold', name, self.updateShaderState)

        opts   .addListener('colourMode',      name, self.updateShaderState)
        opts   .addListener('colourMap',       name, self.cmapUpdate)
        opts   .addListener('xColour',         name, self.updateShaderState)
        opts   .addListener('yColour',         name, self.updateShaderState)
        opts   .addListener('zColour',         name, self.updateShaderState)
        display.addListener('alpha',           name, self.cmapUpdate)
        display.addListener('brightness',      name, self.cmapUpdate)
        display.addListener('contrast',        name, self.cmapUpdate)

    
    def removeListeners(self):

        display = self.display
        opts    = self.displayOpts
        name    = self.name

        opts   .removeListener('resolution',      name)
        opts   .removeListener('transform',       name)

        opts   .removeListener('shResolution',    name)
        opts   .removeListener('size',            name)
        opts   .removeListener('lighting',        name)
        opts   .removeListener('neuroFlip',       name)
        opts   .removeListener('radiusThreshold', name)
        
        opts   .removeListener('colourMode',      name)
        opts   .removeListener('colourMap',       name)
        opts   .removeListener('xColour',         name)
        opts   .removeListener('yColour',         name)
        opts   .removeListener('zColour',         name)
        display.removeListener('alpha',           name)
        display.removeListener('brightness',      name)
        display.removeListener('contrast',        name)

        
    def updateShaderState(self, *a):
        if fslgl.glsh_funcs.updateShaderState(self):
            self.notify()
            return True
        return False


    def cmapUpdate(self, *a):

        opts    = self.displayOpts
        display = self.display

        # The cmapTexture is used when
        # colouring by radius values,
        # which are assumed to lie between
        # 0.0 and 1.0
        dmin, dmax = fslcm.briconToDisplayRange(
            (0.0, 1.0),
            display.brightness / 100.0,
            display.contrast   / 100.0)

        self.cmapTexture.set(cmap=opts.colourMap,
                             alpha=display.alpha / 100.0,
                             displayRange=(dmin, dmax))

        if not self.updateShaderState():
            self.notify()

        
    def shResChanged(self, *a):
        """Called when the :attr:`.SHOpts.shResolution` property changes.
        """
        self.coefficients = self.displayOpts.getCoefficients()


    def updateRadTexture(self, voxels):

        # Remove out-of-bounds voxels
        shape   = self.image.shape[:3]
        x, y, z = voxels.T

        out = (x <  0)        | \
              (y <  0)        | \
              (z <  0)        | \
              (x >= shape[0]) | \
              (y >= shape[1]) | \
              (z >= shape[2])

        x = np.array(x[~out], dtype=np.int32)
        y = np.array(y[~out], dtype=np.int32)
        z = np.array(z[~out], dtype=np.int32)

        # We need to [insert description here when you know more
        #             about the topic].
        # This can be done with a straight matrix multiplication.
        coef  = self.coefficients
        data  = self.image.nibImage.get_data()[x, y, z, :]
        radii = np.dot(coef, data.T).flatten(order='F')

        # The radii are interpreted as a 1D vector
        # containing the radii for every vertex
        # in every voxel. Because we're using an
        # old version of OpenGL (2.1), we can't
        # simply make this 1D vector available
        # to the vertex shader - we need to copy
        # it into a texture, and that texture has
        # to be 3D, because texture dimensions
        # have a maximum size limit.
        #
        # So here we are calculating a suitable
        # 3D shape in which the radius values can
        # be stored. The shape may end up being
        # larger than necessary, if the number of
        # voxels cannot easily be divided/dispersed
        # across the other dimensions.
        radTexShape = np.array(list(radii.shape) + [1, 1])
        maxTexSize  = gl.glGetIntegerv(gl.GL_MAX_3D_TEXTURE_SIZE)
        while np.any(radTexShape > maxTexSize):

            # Find the biggest and smallest dimensions
            imin = np.argmin(radTexShape)
            imax = np.argmax(radTexShape)

            # Try to find a way to move values
            # from the biggest dimension to the
            # smallest dimension
            divisor = 0
            for i in (2, 3, 5, 7):
                if radTexShape[imax] % i == 0:
                    divisor = i
                    break

            # If we can't evenly reshape the texture
            # dimensions, we have to increase the
            # texture size - the radius data will 
            # only take up a portion of the allocated
            # texture size. 
            else:
                divisor            = 2
                radTexShape[imax] += 1

            radTexShape[imax] /= divisor
            radTexShape[imin] *= divisor

        # Resize and reshape the radius
        # array as needed
        radTexSize = np.prod(radTexShape)
        if radTexSize != radii.size:
            radii.resize(radTexSize)
            
        radii = radii.reshape(radTexShape, order='F')

        # Copy the data to the texture
        self.radTexture.set(data=radii)

        return radTexShape


    def ready(self):
        return self.radTexture.ready()


    def preDraw(self):

        # The radTexture needs to be bound *last*
        self.cmapTexture.bindTexture(gl.GL_TEXTURE1)
        self.radTexture .bindTexture(gl.GL_TEXTURE0)
        
        fslgl.glsh_funcs.preDraw(self)


    def draw(self, zpos, xform=None, bbox=None):
        fslgl.glsh_funcs.draw(self, zpos, xform, bbox)


    def postDraw(self):
        self.radTexture .unbindTexture()
        self.cmapTexture.unbindTexture()
        fslgl.glsh_funcs.postDraw(self)
