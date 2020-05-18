from functools import partial

from .constraint import PositivityConstraint, MonotonicityConstraint, SymmetryConstraint, L0Constraint
from .constraint import NormalizationConstraint, ConstraintChain, CenterOnConstraint
from .parameter import Parameter, relative_step
from .component import ComponentTree, FunctionComponent, FactorizedComponent
from .bbox import Box
from .wavelet import Starlet, mad_wavelet
from .observation import Observation
from .interpolation import interpolate_observation
from . import operator

# make sure that import * above doesn't import its own stock numpy
import autograd.numpy as np

import logging

logger = logging.getLogger("scarlet.source")


def get_pixel_sed(sky_coord, observation):
    """Get the SED at `sky_coord` in `observation`

    Parameters
    ----------
    sky_coord: tuple
        Position in the observation
    observation: `~scarlet.Observation`
        Observation to extract SED from.

    Returns
    -------
    SED: `~numpy.array`
    """

    pixel = observation.frame.get_pixel(sky_coord)
    sed = observation.images[:, pixel[0], pixel[1]].copy()
    return sed


def get_psf_sed(sky_coord, observation, frame):
    """Get SED for a point source at `sky_coord` in `observation`

    Identical to `get_pixel_sed`, but corrects for the different
    peak values of the observed seds to approximately correct for PSF
    width variations between channels.

    Parameters
    ----------
    sky_coord: tuple
        Position in the observation
    observation: `~scarlet.Observation`
        Observation to extract SED from.
    frame: `~scarlet.Frame`
        Frame of the model

    Returns
    -------
    SED: `~numpy.array`
    """
    sed = get_pixel_sed(sky_coord, observation)

    # approx. correct PSF width variations from SED by normalizing heights
    if observation.frame.psf is not None:
        # Account for the PSF in the intensity
        sed /= observation.frame.psf.image.max(axis=(1, 2))

    if frame.psf is not None:
        sed = sed * frame.psf.image[0].max()

    return sed

def build_sed_coadd(seds, bg_rmses, observations):
    """Build a channel weighted coadd to use for source detection
    Parameters
    ----------
    sed: array
        SED at the center of the source.
    bg_rms: array
        Background RMS in each channel in observation.
    observations: list of `~scarlet.observation.Observation`
        Observations to use for the coadd.
    Returns
    -------
    detect: array
        2D image created by weighting all of the channels by SED
    bg_cutoff: float
        The minimum value in `detect` to include in detection.
    """
    # The observation that lives in the same plane as the frame
    loc = np.where([type(obs) is Observation for obs in observations])
    # If more than one element is an `Observation`, then pick the first one as a reference (arbitrary)
    obs_ref = observations[loc[0][0]]
    positive_img = []
    positive_bgrms = []
    weights = []
    jacobian_args = []
    for i,obs in enumerate(observations):
        sed = seds[i]
        C = len(seds[i])
        bg_rms = bg_rmses[i]
        if np.any(bg_rms <= 0):
            raise ValueError("bg_rms must be greater than zero in all channels")

        positive = [c for c in range(C) if sed[c] > 0]
        if type(obs) is Observation:
            positive_img += [obs.images[c] for c in positive]
        else:
            positive_img += [interpolate_observation(obs, obs_ref.frame)[c] for c in positive]
        positive_bgrms += [bg_rms[c] for c in positive]
        weights += [sed[c] / bg_rms[c] ** 2 for c in positive]
        jacobian_args += [sed[c] ** 2 / bg_rms[c] ** 2 for c in positive]

    detect = np.einsum("i,i...", np.array(weights), positive_img) / np.sum(jacobian_args)

    # thresh is multiple above the rms of detect (weighted variance across channels)
    bg_cutoff = np.sqrt((np.array(weights) ** 2 * np.array(positive_bgrms) ** 2).sum()) / np.sum(jacobian_args)
    return detect, bg_cutoff

def get_best_fit_seds(morphs, images):
    """Calculate best fitting SED for multiple components.

    Solves min_A ||img - AS||^2 for the SED matrix A,
    assuming that the images only contain a single source.

    Parameters
    ----------
    morphs: list
        Morphology for each component in the source.
    frame: `scarlet.observation.frame`
        The frame of the model
    images: array
        Observation to extract SEDs from.

    Returns
    -------
    SED: `~numpy.array`
    """
    K = len(morphs)
    _morph = morphs.reshape(K, -1)
    data = images.reshape(images.shape[0], -1)
    seds = np.dot(np.linalg.inv(np.dot(_morph, _morph.T)), np.dot(_morph, data.T))
    return seds

def trim_morphology(sky_coord, frame, morph, bg_cutoff, thresh):
    # trim morph to pixels above threshold
    mask = morph > bg_cutoff * thresh
    boxsize = 16
    pixel_center = frame.get_pixel(sky_coord)
    if mask.sum() > 0:
        morph[~mask] = 0

        # normalize to unity at peak pixel
        center_morph = morph[pixel_center[0], pixel_center[1]]
        morph /= center_morph

        # find fitting bbox
        bbox = Box.from_data(morph, min_value=0)
        if bbox.contains(pixel_center):
            size = 2 * max(
                (
                    pixel_center[0] - bbox.start[-2],
                    bbox.stop[0] - pixel_center[-2],
                    pixel_center[1] - bbox.start[-1],
                    bbox.stop[1] - pixel_center[-1],
                )
            )
            while boxsize < size:
                boxsize *= 2
    else:
        msg = "No flux above threshold for source at y={0} x={1}".format(*pixel_center)
        logger.warning(msg)

    # define bbox and trim to bbox
    bottom = pixel_center[0] - boxsize // 2
    top = pixel_center[0] + boxsize // 2
    left = pixel_center[1] - boxsize // 2
    right = pixel_center[1] + boxsize // 2
    bbox = Box.from_bounds((bottom, top), (left, right))
    morph = bbox.extract_from(morph)
    bbox_3d = Box.from_bounds((0, frame.C), (bottom, top), (left, right))
    return morph, bbox_3d


def init_extended_source(
    sky_coord,
    frame,
    observations,
    coadd = None,
    bg_cutoff=None,
    thresh=1,
    symmetric=True,
    monotonic=True,
    min_grad=0.2,
):
    """Initialize the source that is symmetric and monotonic
    See `ExtendedSource` for a description of the parameters
    """
    try:
        iter(observations)
    except TypeError:
        observations = [observations]
    # determine initial SED from peak position
    # SED in the frame for source detection
    seds = []
    for obs in observations:
        _sed = get_psf_sed(sky_coord, obs, frame)
        seds.append(_sed)
    sed = np.concatenate(seds).flatten()

    if np.all(sed <= 0):
        # If the flux in all channels is  <=0,
        msg = f"Zero or negative SED {sed} at y={sky_coord[0]}, x={sky_coord[1]}"
        logger.warning(msg)

    if coadd is None:
        # which observation to use for detection and morphology
        try:
            bg_rms = np.array([[1 / np.sqrt(w[w > 0].mean()) for w in obs_.weights] for obs_ in observations])
        except:
            raise AttributeError(
                "Observation.weights missing! Please set inverse variance weights"
            )
        coadd, bg_cutoff = build_sed_coadd(seds, bg_rms, observations)
    else:
        if bg_cutoff is None:
            raise AttributeError(
                "background cutoff missing! Please set argument bg_cutoff"
            )

    # Apply the necessary constraints
    center = frame.get_pixel(sky_coord)
    if symmetric:
        morph = operator.prox_uncentered_symmetry(
            coadd*1., 0, center=center, algorithm="sdss" # *1 is to artificially pass a variable that is not coadd
        )
    else:
        morph = coadd
    if monotonic:
        # use finite thresh to remove flat bridges
        prox_monotonic = operator.prox_weighted_monotonic(
            morph.shape, neighbor_weight="flat", center=center, min_gradient=min_grad
        )
        morph = prox_monotonic(morph, 0).reshape(morph.shape)

    morph, bbox = trim_morphology(sky_coord, frame, morph, bg_cutoff, thresh)
    return sed, morph, bbox


def init_multicomponent_source(
    sky_coord,
    frame,
    observations,
    coadd = None,
    bg_cutoff = None,
    flux_percentiles=None,
    thresh=1,
    symmetric=True,
    monotonic=True,
    min_grad=0.2,
):
    """Initialize multiple components
    See `MultiComponentSource` for a description of the parameters
    """
    try:
        iter(observations)
    except TypeError:
        observations = [observations]

    # The observation that lives in the same plane as the frame
    loc = np.where([type(obs) is Observation for obs in observations])
    # If more than one element is an `Observation`, then pick the first one as a reference (arbitrary)
    obs_ref = observations[loc[0][0]]

    if flux_percentiles is None:
        flux_percentiles = [25]

    # Initialize the first component as an extended source
    sed, morph, bbox = init_extended_source(
        sky_coord,
        frame,
        observations,
        coadd=coadd,
        bg_cutoff=bg_cutoff,
        thresh=thresh,
        symmetric=symmetric,
        monotonic=monotonic,
        min_grad=min_grad
    )
    # create a list of components from base morph by layering them on top of
    # each other so that they sum up to morph
    K = len(flux_percentiles) + 1

    Ny, Nx = morph.shape
    morphs = np.zeros((K, Ny, Nx), dtype=morph.dtype)
    morphs[0, :, :] = morph[:, :]
    max_flux = morph.max()
    percentiles_ = np.sort(flux_percentiles)
    last_thresh = 0
    for k in range(1, K):
        perc = percentiles_[k - 1]
        flux_thresh = perc * max_flux / 100
        mask_ = morph > flux_thresh
        morphs[k - 1][mask_] = flux_thresh - last_thresh
        morphs[k][mask_] = morph[mask_] - flux_thresh
        last_thresh = flux_thresh

    # renormalize morphs: initially Smax
    for k in range(K):
        if np.all(morphs[k] <= 0):
            msg = "Zero or negative morphology for component {} at y={}, x={}"
            logger.warning(msg.format(k, *sky_coord))
        morphs[k] /= morphs[k].max()

    # optimal SEDs given the morphologies, assuming img only has that source
    boxed_img = bbox.extract_from(obs_ref.images)
    seds = get_best_fit_seds(morphs, boxed_img)

    for k in range(K):
        if np.all(seds[k] <= 0):
            # If the flux in all channels is  <=0,
            # the new sed will be filled with NaN values,
            # which will cause the code to crash later
            msg = "Zero or negative SED {} for component {} at y={}, x={}".format(
                seds[k], k, *sky_coord
            )
            logger.warning(msg)

    return seds, morphs, bbox

class Random:
    """ Class used to instantiate a RandomSource class from its kwargs.
    """
    def __init__(self, observation):
        self.kwargs = observation

    def __call__(self, *args):
        """Sets a *Source with all its arguments"""
        return RandomSource(*args, self.kwargs)

class RandomSource(FactorizedComponent):
    """Sources with uniform random morphology and sed.

    For cases with no well-defined spatial shape, this source initializes
    a uniform random field and (optionally) matches the SED to match a given
    observation.
    """

    def __init__(self, model_frame, observation=None):
        """Source intialized as random field.

        Parameters
        ----------
        frame: `~scarlet.Frame`
            The frame of the model
        observation: list of `~scarlet.Observation`
            Observation to initialize the SED of the source
        """
        C, Ny, Nx = model_frame.shape
        morph = np.random.rand(Ny, Nx)

        if observation is None:
            sed = np.random.rand(C)
        else:
            sed = get_best_fit_seds(morph[None], observation.images)[0]

        constraint = PositivityConstraint()
        sed = Parameter(sed, name="sed", step=relative_step, constraint=constraint)
        morph = Parameter(
            morph, name="morph", step=relative_step, constraint=constraint
        )

        super().__init__(model_frame, model_frame.bbox, sed, morph)

class Point:
    """ Class used to instantiate a PointSource class from its kwargs.
    """
    def __call__(self, *args):
        return PointSource(*args)

class PointSource(FunctionComponent):
    """Source intialized with a single pixel

    Point sources are initialized with the SED of the center pixel,
    and the morphology taken from `frame.psfs`, centered at `sky_coord`.
    """

    def __init__(self, model_frame, sky_coord, observations):
        """Source intialized with a single pixel

        Parameters
        ----------
        frame: `~scarlet.Frame`
            The frame of the full model
        sky_coord: tuple
            Center of the source
        observations: instance or list of `~scarlet.Observation`
            Observation(s) to initialize this source
        """
        C, Ny, Nx = model_frame.shape
        self.center = np.array(model_frame.get_pixel(sky_coord), dtype="float")

        # initialize SED from sky_coord
        try:
            iter(observations)
        except TypeError:
            observations = [observations]

        # determine initial SED from peak position
        # SED in the frame for source detection
        seds = []
        for obs in observations:
            _sed = get_psf_sed(sky_coord, obs, model_frame)
            seds.append(_sed)
        sed = np.concatenate(seds).reshape(-1)

        if np.any(sed <= 0):
            # If the flux in all channels is  <=0,
            # the new sed will be filled with NaN values,
            # which will cause the code to crash later
            msg = "Zero or negative SED {} at y={}, x={}".format(sed, *sky_coord)
            if np.all(sed <= 0):
                logger.warning(msg)
            else:
                logger.info(msg)

        # set up parameters
        sed = Parameter(
            sed,
            name="sed",
            step=partial(relative_step, factor=1e-2),
            constraint=PositivityConstraint(),
        )
        center = Parameter(self.center, name="center", step=1e-1)

        # define bbox
        pixel_center = tuple(np.round(center).astype("int"))
        front, back = 0, C
        bottom = pixel_center[0] - model_frame.psf.shape[1] // 2
        top = pixel_center[0] + model_frame.psf.shape[1] // 2
        left = pixel_center[1] - model_frame.psf.shape[2] // 2
        right = pixel_center[1] + model_frame.psf.shape[2] // 2
        bbox = Box.from_bounds((front, back), (bottom, top), (left, right))

        super().__init__(model_frame, bbox, sed, center, self._psf_wrapper)

    def _psf_wrapper(self, *parameters):
        return self.model_frame.psf.__call__(*parameters, bbox=self.bbox)[0]

class Starlets:
    """ "Setter" class to initialise a `StarletSource` from its keyword arguments.

        Attributes
        ----------
        thresh: `float`
            Multiple of the backround RMS used as a
            flux cutoff for morphology initialization.
        starlet_thresh: `float`
            threshold for wavelet coefficients in units of the noise std.
    """
    def __init__(
        self,
        thresh=1.0,
        starlet_thresh=5,
        min_grad = 0,
    ):
        self.kwargs = (thresh,  starlet_thresh, min_grad)

    def __call__(self, *args, coadd = None, bg_cutoff = None):
        """Sets a *Source with all its arguments"""
        return StarletSource(*args, coadd, bg_cutoff, *self.kwargs)

class StarletSource(FunctionComponent):
    """Source intialized with starlet coefficients.

    Sources are initialized with the SED of the center pixel,
    and the morphologies are initialised as ExtendedSources
    and transformed into starlet coefficients.
    """
    def __init__(
        self,
        frame,
        sky_coord,
        observations,
        coadd=None,
        bg_cutoff=None,
        thresh=1.0,
        starlet_thresh = 5,
        min_grad = 0,
    ):
        """Extended source intialized to match a set of observations

        Parameters
        ----------
        frame: `~scarlet.Frame`
            The frame of the model
        sky_coord: tuple
            Center of the source
        observations: instance or list of `~scarlet.observation.Observation`
            Observation(s) to initialize this source.
        obs_idx: int
            Index of the observation in `observations` to
            initialize the morphology.
        thresh: `float`
            Multiple of the backround RMS used as a
            flux cutoff for morphology initialization.
        shifting: `bool`
            Whether or not a subpixel shift is added as optimization parameter
        """
        center = np.array(frame.get_pixel(sky_coord), dtype="float")
        self.pixel_center = tuple(np.round(center).astype("int"))

        # initialize SED from sky_coord
        try:
            iter(observations)
        except TypeError:
            observations = [observations]

        # initialize from observation
        sed, image_morph, bbox = init_extended_source(
            sky_coord,
            frame,
            observations,
            coadd=coadd,
            bg_cutoff=bg_cutoff,
            thresh=thresh,
            symmetric=True,
            monotonic=True,
            min_grad = min_grad,
        )
        noise =[]
        for obs in observations:
            noise += [mad_wavelet(obs.images) * \
                    np.sqrt(np.sum(obs._diff_kernels.image**2, axis = (-2,-1)))]
        noise = np.concatenate(noise)
        # Threshold in units of noise
        thresh = starlet_thresh * np.sqrt(np.sum((sed*noise) ** 2))

        # Starlet transform of morphologies (n1,n2) with 4 dimensions: (1,lvl,n1,n2), lvl = wavelet scales
        self.transform = Starlet(image_morph)
        #The starlet transform is the model
        morph = self.transform.coefficients
        # wavelet-scale norm
        starlet_norm = self.transform.norm
        #One threshold per wavelet scale: thresh*norm
        thresh_array = np.zeros(morph.shape) + thresh
        thresh_array = thresh_array * np.array([starlet_norm])[..., np.newaxis, np.newaxis]
        # We don't threshold the last scale
        thresh_array[:,-1,:,:] = 0

        sed = Parameter(
            sed,
            name="sed",
            step=partial(relative_step, factor=1e-2),
            constraint=PositivityConstraint(),
        )

        morph_constraint = ConstraintChain(*[L0Constraint(thresh_array), PositivityConstraint()])

        morph = Parameter(morph, name="morph", step=1.e-2, constraint=morph_constraint)

        super().__init__(frame, bbox, sed, morph, self._iuwt)

    @property
    def center(self):
        if len(self.parameters) == 3:
            return self.pixel_center + self.shift
        else:
            return self.pixel_center

    def _iuwt(self, param):
        """ Takes the inverse transform of parameters as starlet coefficients.

        """
        return Starlet(coefficients = param).image[0]

class Extended:
    """ "Setter" class to initialise an `ExtendedSource` from its keyword arguments only

        Attributes
        ----------
        thresh: `float`
            Multiple of the backround RMS used as a
            flux cutoff for morphology initialization.
        monotonic: ['flat', 'angle', 'nearest'] or None
            Which version of monotonic decrease in flux from the center to enforce
        symmetric: `bool`
            Whether or not to enforce symmetry.
        shifting: `bool`
            Whether or not a subpixel shift is added as optimization parameter
        min_grad: `float`
            sets the "strength" of the monotonicity operator (default = 0.2 is strong!)
    """
    def __init__(
        self,
        thresh=1.0,
        monotonic="flat",
        symmetric=False,
        shifting=False,
        min_grad = 0.2
    ):
        self.kwargs = (thresh,  monotonic,  symmetric, shifting, min_grad)

    def __call__(self, *args, coadd = None, bg_cutoff = None):
        """Sets a *Source with all its arguments"""
        return ExtendedSource(*args, coadd, bg_cutoff, *self.kwargs)

class ExtendedSource(FactorizedComponent):
    def __init__(
        self,
        model_frame,
        sky_coord,
        observations,
        coadd=None,
        bg_cutoff=None,
        thresh=1.0,
        monotonic="flat",
        symmetric=False,
        shifting=False,
        min_grad = 0.2
    ):
        """Extended source intialized to match a set of observations

        Parameters
        ----------
        model_frame: `~scarlet.Frame`
            The frame of the full model
        sky_coord: tuple
            Center of the source
        observations: instance or list of `~scarlet.observation.Observation`
            Observation(s) to initialize this source.
        coadd: `numpy.ndarray`
            The coaddition of all images across observations.
        bg_cutoff: float
            flux cutoff for morphology initialization.
        thresh: `float`
            Multiple of the backround RMS used as a
            flux cutoff for morphology initialization.
        monotonic: ['flat', 'angle', 'nearest'] or None
            Which version of monotonic decrease in flux from the center to enforce
        symmetric: `bool`
            Whether or not to enforce symmetry.
        shifting: `bool`
            Whether or not a subpixel shift is added as optimization parameter
        """
        center = np.array(model_frame.get_pixel(sky_coord), dtype="float")
        self.pixel_center = tuple(np.round(center).astype("int"))

        if shifting:
            shift = Parameter(center - self.pixel_center, name="shift", step=1e-1)
        else:
            shift = None

        # initialize from observation
        sed, morph, bbox = init_extended_source(
            sky_coord,
            model_frame,
            observations,
            coadd,
            bg_cutoff,
            thresh=thresh,
            symmetric=True,
            monotonic=True,
            min_grad = min_grad
        )

        sed = Parameter(
            sed,
            name="sed",
            step=partial(relative_step, factor=1e-2),
            constraint=PositivityConstraint(),
        )

        constraints = []

        # backwards compatibility: monotonic was boolean
        if monotonic is True:
            monotonic = "angle"
        elif monotonic is False:
            monotonic = None
        if monotonic is not None:
            # most astronomical sources are monotonically decreasing
            # from their center
            constraints.append(MonotonicityConstraint(neighbor_weight=monotonic, min_gradient=min_grad))

        if symmetric:
            # have 2-fold rotation symmetry around their center ...
            constraints.append(SymmetryConstraint())

        constraints += [
            # ... and are positive emitters
            PositivityConstraint(),
            # prevent a weak source from disappearing entirely
            # CenterOnConstraint(),
            # break degeneracies between sed and morphology
            NormalizationConstraint("max"),
        ]
        morph_constraint = ConstraintChain(*constraints)

        morph = Parameter(morph, name="morph", step=1e-2, constraint=morph_constraint)

        super().__init__(model_frame, bbox, sed, morph, shift=shift)

    @property
    def center(self):
        if len(self.parameters) == 3:
            return self.pixel_center + self.shift
        else:
            return self.pixel_center

class MultiComponent:
    """ "Setter" class to initialise an `ExtendedSource` from its keyword arguments only

            Attributes
            ----------
            thresh: `float`
                Multiple of the backround RMS used as a
                flux cutoff for morphology initialization.
            monotonic: ['flat', 'angle', 'nearest'] or None
                Which version of monotonic decrease in flux from the center to enforce
            symmetric: `bool`
                Whether or not to enforce symmetry.
            shifting: `bool`
                Whether or not a subpixel shift is added as optimization parameter
            min_grad: `float`
                sets the "strength" of the monotonicity operator (default = 0.2 is strong!)
        """

    def __init__(
            self,
            thresh=1.0,
            flux_percentiles=None,
            monotonic="flat",
            symmetric=False,
            shifting=False,
            min_grad=0.2
    ):
        self.kwargs = (thresh, flux_percentiles, monotonic, symmetric, shifting, min_grad)

    def __call__(self, *args, coadd=None, bg_cutoff=None):
        """Sets a *Source with all its arguments"""
        return MultiComponentSource(*args, coadd, bg_cutoff, *self.kwargs)

class MultiComponentSource(ComponentTree):
    """Extended source with multiple components layered vertically.

    Uses `~scarlet.source.ExtendedSource` to define the overall morphology,
    then erodes the outer footprint until it reaches the specified size percentile.
    For the narrower footprint, it evaluates the mean value at the perimeter and
    sets the inside to the perimeter value, creating a flat distribution inside.
    The subsequent component(s) is/are set to the difference between the flattened
    and the overall morphology.
    The SED for all components is calculated as the best fit of the multi-component
    morphology to the multi-channel image in the region of the source.
    """

    def __init__(
        self,
        model_frame,
        sky_coord,
        observations,
        thresh=1.0,
        coadd=None,
        bg_cutoff=None,
        flux_percentiles=None,
        symmetric=False,
        monotonic="flat",
        shifting=False,
        min_grad = 0.2,
    ):
        """Create multi-component extended source.

        Parameters
        ----------
        model_frame: `~scarlet.Frame`
            The frame of the full model
        sky_coord: tuple
            Center of the source
        observations: instance or list of `~scarlet.observation.Observation`
            Observation(s) to initialize this source.
        obs_idx: int
            Index of the observation in `observations` to
            initialize the morphology.
        thresh: `float`
            Multiple of the backround RMS used as a
            flux cutoff for morphology initialization.
        flux_percentiles: list
            The flux percentile of each component. If `flux_percentiles` is `None`
            then `flux_percentiles=[25]`, a single component with 25% of the flux
            as the primary source.
        symmetric: `bool`
            Whether or not to enforce symmetry.
        monotonic: ['flat', 'angle', 'nearest'] or None
            Which version of monotonic decrease in flux from the center to enforce
        shifting: `bool`
            Whether or not a subpixel shift is added as optimization parameter
        """
        self.symmetric = symmetric
        self.monotonic = monotonic
        self.coords = sky_coord
        center = np.array(model_frame.get_pixel(sky_coord), dtype="float")
        pixel_center = tuple(np.round(center).astype("int"))

        if shifting:
            shift = Parameter(center - pixel_center, name="shift", step=1e-1)
        else:
            shift = None

        # initialize from observation
        seds, morphs, bbox = init_multicomponent_source(
            sky_coord,
            model_frame,
            observations,
            coadd=None,
            bg_cutoff=bg_cutoff,
            flux_percentiles=flux_percentiles,
            thresh=thresh,
            symmetric=True,
            monotonic=True,
            min_grad=min_grad,
        )

        constraints = []

        # backwards compatibility: monotonic was boolean
        if monotonic is True:
            monotonic = "angle"
        elif monotonic is False:
            monotonic = None
        if monotonic is not None:
            # most astronomical sources are monotonically decreasing
            # from their center
            constraints.append(MonotonicityConstraint(neighbor_weight=monotonic, min_gradient=min_grad))

        if symmetric:
            # have 2-fold rotation symmetry around their center ...
            constraints.append(SymmetryConstraint())
        constraints += [
            # ... and are positive emitters
            PositivityConstraint(),
            # prevent a weak source from disappearing entirely
            CenterOnConstraint(),
            # break degeneracies between sed and morphology
            NormalizationConstraint("max"),
        ]
        morph_constraint = ConstraintChain(*constraints)

        components = []
        for k in range(len(seds)):
            sed = Parameter(
                seds[k],
                name="sed",
                step=partial(relative_step, factor=1e-1),
                constraint=PositivityConstraint(),
            )
            morph = Parameter(
                morphs[k], name="morph", step=1e-2, constraint=morph_constraint
            )
            components.append(
                FactorizedComponent(model_frame, bbox, sed, morph, shift=shift)
            )
            components[-1].pixel_center = pixel_center
        super().__init__(components)

    @property
    def shift(self):
        c = self.components[0]
        return c.shift

    @property
    def center(self):
        c = self.components[0]
        if len(c.parameters) == 3:
            return c.pixel_center + c.shift
        else:
            return c.pixel_center
