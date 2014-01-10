import numpy as np
from numpy import pi, sin, cos, arctan2, trapz, array
from mbwind import Element
from scipy.interpolate import interp1d


class AerofoilDatabase(object):
    def __init__(self, filename):
        self.filename = filename
        self.aerofoils = np.load(filename)

        # Set up interpolating functions for each thickness
        datasets = self.aerofoils['datasets']
        self._CLs = [interp1d(data['alpha'], data['CL']) for data in datasets]
        self._CDs = [interp1d(data['alpha'], data['CD']) for data in datasets]

    def for_thickness(self, thickness):
        thicknesses = self.aerofoils['thicknesses']
        def CL(alpha):
            CL_thick = interp1d(thicknesses, [CL(alpha) for CL in self._CLs])
            return CL_thick(thickness)
        def CD(alpha):
            CD_thick = interp1d(thicknesses, [CD(alpha) for CD in self._CDs])
            return CD_thick(thickness)
        return Aerofoil('%02d%% thickness' % (100 * thickness), CL, CD)


class Aerofoil(object):
    def __init__(self, name, CL, CD):
        self.name = name
        self.CL = CL
        self.CD = CD


class BladeSection(object):
    def __init__(self, chord, twist, foil):
        self.chord = chord
        self.twist = twist
        self.foil = foil

    def force_coefficients(self, inflow_angle):
        # lift & drag coefficients
        alpha = inflow_angle - self.twist
        cl = self.foil.CL(alpha)
        cd = self.foil.CD(alpha)

        # resolve in- and out-of-plane
        cx =  cl*cos(inflow_angle) + cd*sin(inflow_angle)
        cy = -cl*sin(inflow_angle) + cd*cos(inflow_angle)

        return cx, cy


def thrust_correction_factor(a):
    """Correction to the thrust for high induction factors"""
    if a <= 0.3539:
        H = 1.0
    else:
        H = 4*a*(1-a) / (0.60 + 0.61*a + 0.79*a**2)
    return H


def iterate_induction_factors(LSR, blade_section, solidity, a, at):
    # inflow angle
    phi = arctan2(1.0 - a, LSR*(1.0 + at))
    cx, cy = blade_section.force_coefficients(phi)

    # calculate new induction factors
    if solidity * cx == 0:
        new_a = 0
    else:
        Kx = 4*sin(phi)**2       / (solidity*cx)
        H = thrust_correction_factor(a)
        new_a  = 1. / (  Kx/H + 1 )

    if solidity * cy == 0:
        new_at = 0
    else:
        Ky = 4*sin(phi)*cos(phi) / (solidity*cy)
        new_at = 1. / ( -Ky   - 1 )

    # Slow down iteration a bit to improve convergence.
    # XXX is there a justification for this?
    new_a = (a + new_a) / 2
    new_at = (at + new_at) / 2

    return (new_a, new_at)


def solve_induction_factors(LSR, blade_section, solidity,
                            tol=1e-4, max_iterations=300):
    """
    Parameters:
     - LSR:      local speed ratio = omega r / U
     - twist:    twist angle of blade
     - solidity: chord solidity = (B c / 2 pi r)
    """
    a = at = 0
    for i in range(max_iterations):
        a1, at1 = iterate_induction_factors(LSR, blade_section, solidity, a, at)
        if abs(a1 - a) < tol and abs(at1 - at) < tol:
            return a1, at1
        a, at = a1, at1
    raise RuntimeError("maximum iterations reached")


class BEMAnnulus(object):
    def __init__(self, radius, chord, twist, foil, num_blades):
        self.radius = radius
        self.blade_section = BladeSection(chord, twist, foil)
        self.num_blades = num_blades

    def solve(self, windspeed, rotorspeed):
        LSR = self.radius * rotorspeed / windspeed
        solidity = (self.num_blades * self.blade_section.chord /
                    (2 * pi * self.radius))
        a, at = solve_induction_factors(LSR, self.blade_section, solidity)
        return a, at

    def forces(self, windspeed, rotorspeed, rho, a, at):
        """Calculate in- and out-of-plane forces per unit length"""

        # Calculate force coefficients
        LSR = self.radius * rotorspeed / windspeed
        phi = arctan2(1.0 - a, LSR*(1.0 + at))
        cx, cy = self.blade_section.force_coefficients(phi)

        # De-normalise to actual forces
        W = windspeed * (1-a) / sin(phi)
        fx = 0.5 * rho * W**2 * self.blade_section.chord * cx
        fy = 0.5 * rho * W**2 * self.blade_section.chord * cy

        return fx, fy


class UnsteadyBEMAnnulus(BEMAnnulus):
    def __init__(self, radius, chord, twist, foil, num_blades, edge_radii):
        super(UnsteadyBEMAnnulus, self).__init__(radius, chord, twist, foil, num_blades)
        self.edge_radii = edge_radii

    def inflow_damping(self):
        """
        Return the part of the 
        $$
        C_T = 4 a (1-a) + \frac{16}{3 \pi U_0} \frac{R_2^3 - R_1^3}{R_2^2 - R_1^2} \dot{a}
        $$
        """


class BEMModel(object):
    def __init__(self, blade, root_length, num_blades, aerofoil_database,
                 bem_radii=None, unsteady=False):

        if bem_radii is None:
            bem_radii = root_length + blade.radii

        self.blade = blade
        self.root_length = root_length
        self.num_blades = num_blades
        self.unsteady = unsteady

        interp_chord = interp1d(root_length + blade.radii, blade.chord)
        interp_twist = interp1d(root_length + blade.radii, blade.twist)
        interp_thick = interp1d(root_length + blade.radii, blade.thickness)
        self.annuli = []
        for i, r in enumerate(bem_radii):
            foil = aerofoil_database.for_thickness(interp_thick(r) / 100)
            if unsteady:
                # Find two ends of strip -- halfway between this point and neighbours,
                # apart from at ends when it's half as wide.
                if i == 0:
                    R1 = (bem_radii[i])
                else:
                    R1 = (bem_radii[i] + bem_radii[i-1]) / 2
                if i == len(bem_radii)-1:
                    R2 = (bem_radii[i])
                else:
                    R2 = (bem_radii[i] + bem_radii[i+1]) / 2
                annulus = UnsteadyBEMAnnulus(r, interp_chord(r), interp_twist(r),
                                             foil, num_blades, (R1, R2))
            else:
                annulus = BEMAnnulus(r, interp_chord(r), interp_twist(r), foil, num_blades)
            self.annuli.append(annulus)

    @property
    def radii(self):
        return [annulus.radius for annulus in self.annuli]

    def solve(self, windspeed, rotorspeed):
        factors = [annulus.solve(windspeed, rotorspeed)
                   for annulus in self.annuli]
        return factors

    def forces(self, windspeed, rotorspeed, rho):
        factors = self.solve(windspeed, rotorspeed)
        forces = [annulus.forces(windspeed, rotorspeed, rho, a, at)
                  for annulus, (a, at) in zip(self.annuli, factors)]

        # Force last station to have zero force for compatibility with Bladed
        # XXX this wouldn't work if the last station isn't guaranteed
        #     to be at the tip
        forces[-1] = (0, 0)

        return forces

    def pcoeffs(self, windspeed, rotorspeed):
        # We'll nondimensionalise again later so value of rho doesn't matter
        forces = self.forces(windspeed, rotorspeed, rho=1)
        fx, fy = zip(*forces)

        # Integrate forces and moments about shaft
        r = self.radii
        thrust = self.num_blades * trapz(fx, x=r)
        torque = self.num_blades * trapz(-array(fy) * r, x=r)
        power  = torque * rotorspeed

        # Nondimensionalise
        A = pi * r[-1]**2
        CT = thrust / (0.5 * 1 * windspeed**2 * A)
        CQ = torque / (0.5 * 1 * windspeed**2 * A * r[-1])
        CP = power  / (0.5 * 1 * windspeed**3 * A)

        return CT, CQ, CP
